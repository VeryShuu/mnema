"""Mnema api: REST (FT-21) + vec-interfeys (FT-22) + APScheduler (6.4).

Zapusk: python -m mnema.api.main
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select

from ..core import registry
from ..core.capabilities import capability_status
from ..core.db import Base, get_engine, get_session
from ..core.jobs_api import cancel_job, create_job
from ..core.models import (AudioFile, Job, Notification, Protocol,
                           ReviewRequest, Segment, SessionEvent, SessionHealth,
                           VoiceProfile)
from ..core.notify import NotifyService
from ..core.pipeline import review_requests_resolved, transition
from ..core.scheduler import import_ics, start_default_jobs
from ..core.secrets_store import get_pwd

app = FastAPI(title="Mnema", version="0.1.0")
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.on_event("startup")
def startup() -> None:
    errors = registry.preflight()
    if errors:
        for e in errors:
            print(f"PREFLIGHT ERROR: {e}", flush=True)
        raise SystemExit(1)
    engine = get_engine()
    Base.metadata.create_all(engine)  # dlya SQLite-starta; dalneye Alembic
    try:
        start_default_jobs()
    except Exception as e:  # noqa: BLE001
        print(f"scheduler start failed: {e}", flush=True)


# ----------------------------------------------------------------- REST (FT-21)
class JobIn(BaseModel):
    meeting_link: str | None = None
    meeting_id: str | None = None
    pwd: str | None = None
    topic: str | None = None
    start_at: dt.datetime | None = None
    duration_max_min: int | None = None


@app.get("/api/jobs")
def api_jobs(status: str | None = None):
    with get_session() as s:
        q = select(Job).order_by(Job.start_at.desc()).limit(200)
        if status:
            q = q.where(Job.status == status)
        jobs = s.scalars(q).all()
        return [{
            "id": j.id, "zoom_meeting_id": j.zoom_meeting_id, "topic": j.topic,
            "start_at": j.start_at, "status": j.status, "fail_reason": j.fail_reason,
            "started_ts": j.started_ts, "ended_ts": j.ended_ts,
        } for j in jobs]


@app.post("/api/jobs")
def api_create_job(body: JobIn):
    job = create_job(meeting_link=body.meeting_link, meeting_id=body.meeting_id,
                     pwd=body.pwd, topic=body.topic or "",
                     start_at=body.start_at,
                     duration_max_min=body.duration_max_min)
    return {"id": job.id, "status": job.status}


@app.delete("/api/jobs/{jid}")
def api_cancel_job(jid: int):
    job = cancel_job(jid)
    if job is None:
        raise HTTPException(404)
    return {"id": job.id, "status": job.status}


@app.get("/api/jobs/{jid}")
def api_job(jid: int):
    with get_session() as s:
        job = s.get(Job, jid)
        if job is None:
            raise HTTPException(404)
        events = s.scalars(select(SessionEvent).where(
            SessionEvent.job_id == jid).order_by(SessionEvent.ts)).all()
        audio = s.scalars(select(AudioFile).where(AudioFile.job_id == jid)).all()
        return {
            "id": job.id, "zoom_meeting_id": job.zoom_meeting_id, "topic": job.topic,
            "status": job.status, "error": job.error, "fail_reason": job.fail_reason,
            "start_at": job.start_at, "started_ts": job.started_ts,
            "ended_ts": job.ended_ts, "note": job.note,
            "events": [{"ts": e.ts, "event": e.event, "detail": e.detail} for e in events],
            "audio": [{"kind": a.kind, "duration_s": a.duration_s, "bytes": a.bytes} for a in audio],
        }


@app.get("/api/jobs/{jid}/join-info")
def api_join_info(jid: int):
    """Vnutrenniy endpoint dlya web-bot: meeting_id + pwd (iz secrets store).
    V setevom perimetre (HT-3); pwd tuda ne pishetsya v public API."""
    with get_session() as s:
        job = s.get(Job, jid)
        if job is None:
            raise HTTPException(404)
        return {"meeting_id": job.zoom_meeting_id, "pwd": get_pwd(job.pwd_ref),
                "join_link": job.join_link}


# vnutrennie sobytiya ot web-bot (FT-9 event-log + statusy)
class EventIn(BaseModel):
    event: str
    detail: str | None = None


class StatusIn(BaseModel):
    status: str
    fail_reason: str | None = None
    error: str | None = None
    attempts: int | None = None


@app.post("/api/jobs/{jid}/events")
def api_job_event(jid: int, body: EventIn):
    with get_session() as s:
        if s.get(Job, jid) is None:
            raise HTTPException(404)
        s.add(SessionEvent(job_id=jid, event=body.event, detail=body.detail))
        s.commit()
    return {"ok": True}


@app.post("/api/jobs/{jid}/status")
def api_job_status(jid: int, body: StatusIn):
    if body.attempts is not None:
        with get_session() as s:
            j = s.get(Job, jid)
            if j is None:
                raise HTTPException(404)
            j.join_attempts = body.attempts
            s.commit()
    try:
        transition(jid, body.status, fail_reason=body.fail_reason, error=body.error,
                   event=f"status:{body.status}")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "status": body.status}


class FinishedIn(BaseModel):
    webm_path: str | None = None
    wav_path: str | None = None
    mp3_path: str | None = None
    duration_s: float | None = None
    sizes: dict[str, int] | None = None


@app.post("/api/jobs/{jid}/finished")
def api_job_finished(jid: int, body: FinishedIn):
    """web-bot soobschaet o zavershenii zapisi: audio_files + processing (FT-34)."""
    from ..core.pipeline import register_recording_finished
    register_recording_finished(jid, webm_path=body.webm_path, duration_s=body.duration_s)
    sizes = body.sizes or {}

    def _add(kind: str, path: str | None) -> None:
        if not path:
            return
        from ..core.models import AudioFile
        with get_session() as s:
            exists = s.scalars(select(AudioFile).where(
                AudioFile.job_id == jid, AudioFile.kind == kind)).first()
            if exists is None:
                s.add(AudioFile(job_id=jid, kind=kind, path=path,
                                bytes=sizes.get(kind), duration_s=body.duration_s))
                s.commit()
    _add("wav", body.wav_path)
    _add("mp3", body.mp3_path)
    transition(jid, "processing", event="recording_finished")
    from rq import Queue
    from ..core.queue import enqueue_job
    processing_started = enqueue_job("asr", "mnema.worker.tasks.run_asr_pipeline", jid)
    if not processing_started:
        pass  # worker vzjanet sam po ocheredi statusov; v dev — run inline
    return {"ok": True}


@app.get("/api/protocols/{pid}")
def api_protocol(pid: int, format: str = "md"):
    with get_session() as s:
        p = s.get(Protocol, pid)
        if p is None:
            raise HTTPException(404)
        if format == "json":
            return p.json_data or {}
        path = p.md_path if format == "md" else p.docx_path
        if not path or not Path(path).exists():
            raise HTTPException(404, "fayl protokola otsutstvuet")
        media = ("text/markdown" if format == "md" else
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        return FileResponse(path, media_type=media, filename=Path(path).name)


@app.get("/api/protocols/{pid}/audio")
def api_protocol_audio(pid: int, format: str = "mp3", request: Request = None):
    """FT-41: mp3|wav|webm s podderzhkoy Range."""
    with get_session() as s:
        p = s.get(Protocol, pid)
        if p is None:
            raise HTTPException(404)
        job_id = p.job_id
    return _serve_audio(job_id, format, request)


@app.get("/api/jobs/{jid}/audio")
def api_job_audio(jid: int, format: str = "webm", request: Request = None):
    """Audio dlya Job (mp3 dostupen srazu posle okonchaniya zapisi — FT-41v)."""
    return _serve_audio(jid, format, request)


def _serve_audio(job_id: int, fmt: str, request: Request | None):
    if fmt not in ("mp3", "wav", "webm"):
        raise HTTPException(400, "format: mp3|wav|webm")
    with get_session() as s:
        a = s.scalars(select(AudioFile).where(
            AudioFile.job_id == job_id, AudioFile.kind == fmt)).first()
    if a is None or not Path(a.path).exists():
        raise HTTPException(404, f"audio {fmt} otsutstvuet")
    path = Path(a.path)
    size = path.stat().st_size
    mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "webm": "audio/webm"}[fmt]
    range_header = request.headers.get("range") if request else None
    if range_header and range_header.startswith("bytes="):
        start_s, _, end_s = range_header[6:].partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)
        length = max(0, end - start + 1)

        def chunks():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    data = f.read(min(64 * 1024, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
        return StreamingResponse(chunks(), status_code=206, media_type=mime, headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        })
    return FileResponse(path, media_type=mime, headers={"Accept-Ranges": "bytes"})


class AlertIn(BaseModel):
    subject: str
    body: str
    dedup_key: str | None = "alert"


@app.post("/api/alert")
def api_alert(body: AlertIn):
    """Vnutrenniy signal ot web-bot (napr. reCAPTCHA, §12) -> e-mail."""
    n = NotifyService().enqueue("alert",
                                f"[Mnema] {body.subject}", body.body,
                                dedup_key=body.dedup_key or "alert")
    NotifyService().dispatch_pending(limit=5)
    return {"queued": n.id if n else None}


class SegmentPatch(BaseModel):
    segments: list[dict]  # [{id, speaker_name, speaker_source?}]


@app.patch("/api/protocols/{pid}/segments")
def api_patch_segments(pid: int, body: SegmentPatch):
    """FT-32: ispravlenie imen segmentov (i v opublikovannom protokole)."""
    with get_session() as s:
        p = s.get(Protocol, pid)
        if p is None:
            raise HTTPException(404)
        updated = 0
        for item in body.segments:
            seg = s.get(Segment, item.get("id"))
            if seg is None or seg.job_id != p.job_id:
                continue
            if "speaker_name" in item and item["speaker_name"]:
                seg.speaker_name = item["speaker_name"]
                seg.speaker_source = "voiceprint"
                seg.needs_review = False
                updated += 1
        s.commit()
    return {"updated": updated}


@app.get("/api/reviews")
def api_reviews(status: str = "pending"):
    with get_session() as s:
        rows = s.scalars(select(ReviewRequest).where(
            ReviewRequest.status == status)).all()
        out = []
        for r in rows:
            cand = s.get(VoiceProfile, r.candidate_profile_id) if r.candidate_profile_id else None
            out.append({"id": r.id, "job_id": r.job_id, "cluster_label": r.cluster_label,
                        "candidate": cand.employee_name if cand else None,
                        "candidate_id": r.candidate_profile_id,
                        "similarity": r.similarity, "status": r.status})
        return out


class ReviewAnswer(BaseModel):
    employee_id: int | None = None
    employee_name: str | None = None
    create_new: bool = False
    answered_by: str | None = "web"


@app.post("/api/reviews/{rid}")
def api_answer_review(rid: int, body: ReviewAnswer):
    """FT-28: otvet operatora; primenyaetsya ko vsemu klasteryu."""
    from ..core.speaker import apply_review_answer
    with get_session() as s:
        r = s.get(ReviewRequest, rid)
        if r is None:
            raise HTTPException(404)
        job_id = r.job_id
        cluster = r.cluster_label
    name = apply_review_answer(rid, body.employee_id, body.employee_name,
                               body.create_new, body.answered_by or "web")
    with get_session() as s:
        pending = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == job_id,
            ReviewRequest.status == "pending")).first()
        done_now = pending is None
    if done_now:
        transition(job_id, "processing", event="reviews_done")
        from ..core.queue import enqueue_job
        enqueue_job("structure", "mnema.worker.tasks.run_structure", job_id)
    return {"ok": True, "name": name, "job_done": done_now}


@app.get("/api/voice-profiles")
def api_profiles():
    with get_session() as s:
        rows = s.scalars(select(VoiceProfile)).all()
        return [{"id": v.id, "employee_name": v.employee_name,
                 "employee_id": v.employee_id,
                 "samples": len(v.sample_paths or []),
                 "consent": v.consent_ts is not None,
                 "match_stats": v.match_stats} for v in rows]


class ProfileIn(BaseModel):
    employee_name: str
    employee_id: str | None = None
    consent: bool = False


@app.post("/api/voice-profiles")
def api_create_profile(body: ProfileIn):
    """FT-29: registratsiya golosa (embedding dosylayetsya cherez samples)."""
    if not body.consent:
        raise HTTPException(400, "GT-31: bez soglasiya profil' ne sozdaetsya")
    with get_session() as s:
        v = VoiceProfile(employee_name=body.employee_name, employee_id=body.employee_id,
                         consent_ts=dt.datetime.now(dt.timezone.utc))
        s.add(v)
        s.commit()
        s.refresh(v)
        return {"id": v.id}


@app.delete("/api/voice-profiles/{vid}")
def api_delete_profile(vid: int):
    """FT-31: udalenie profilya + etalonov i embeddingov."""
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        if v is None:
            raise HTTPException(404)
        base = os.environ.get("DATA_DIR", "/data")
        for p in (v.sample_paths or []):
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        s.delete(v)
        s.commit()
    return {"ok": True}


@app.post("/api/voice-profiles/{vid}/sample")
async def api_upload_sample(vid: int, file: UploadFile = File(...)):
    """Zapis' s mikrofona brauzera (FT-29a, MediaRecorder webm) -> embedding."""
    dest_dir = Path(os.environ.get("DATA_DIR", "/data")) / "voice_samples" / str(vid)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"sample_{dt.datetime.now(dt.timezone.utc):%Y%m%d_%H%M%S}.webm"
    dest.write_bytes(await file.read())
    from ..core.speaker import compute_embedding_for_file, quality_gate
    ok, reason = quality_gate(dest)
    if not ok:
        dest.unlink(missing_ok=True)
        return {"ok": False, "reason": reason}
    emb = compute_embedding_for_file(dest)
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        if v is None:
            raise HTTPException(404)
        paths = list(v.sample_paths or []) + [str(dest)]
        v.sample_paths = paths
        if emb:
            existing = v.embedding or []
            v.embedding = emb if not existing else [
                (e + n) / 2 for e, n in zip(existing, emb)]
        s.commit()
    return {"ok": True}


@app.get("/health")
def health():
    caps = capability_status()
    session_ok = None
    with get_session() as s:
        h = s.scalars(select(SessionHealth).order_by(SessionHealth.checked_ts.desc())).first()
        if h:
            session_ok = {"ok": h.ok, "detail": h.detail, "checked_ts": h.checked_ts}
        queue_ok = None
    worker_up = None
    try:
        from redis import Redis
        from ..core.queue import redis_url
        R = Redis.from_url(redis_url(), socket_connect_timeout=1)
        R.ping()
        queue_ok = True
    except Exception:
        queue_ok = False
    llm = caps["llm"]
    asr = caps["asr"]
    return {
        "status": "ok",
        "capabilities": {
            "llm": llm, "asr": asr,
        },
        "zoom_session": session_ok,
        "queue": queue_ok,
    }


# ----------------------------------------------------------------- WEB (FT-22)
@app.get("/", response_class=HTMLResponse)
def web_dashboard(request: Request):
    caps = capability_status()
    with get_session() as s:
        active = s.scalars(select(Job).where(
            Job.status.in_(("scheduled", "launching", "recording", "processing"))
        ).order_by(Job.start_at).limit(20)).all()
        recent_failed = s.scalars(select(Job).where(
            Job.status == "failed").order_by(Job.ended_ts.desc()).limit(10)).all()
        h = s.scalars(select(SessionHealth).order_by(SessionHealth.checked_ts.desc())).first()
    queue_ok = _queue_ok()
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "caps": caps, "active": active,
        "recent_failed": recent_failed, "zoom_health": h, "queue_ok": queue_ok,
        "job_status_counts": _status_counts()})


def _queue_ok() -> bool:
    try:
        from redis import Redis
        from ..core.queue import redis_url
        Redis.from_url(redis_url(), socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


def _status_counts() -> dict:
    from sqlalchemy import func
    with get_session() as s:
        rows = s.execute(select(Job.status, func.count()).group_by(Job.status)).all()
        return dict(rows)


@app.get("/jobs", response_class=HTMLResponse)
def web_jobs(request: Request, status: str | None = None):
    with get_session() as s:
        q = select(Job).order_by(Job.start_at.desc()).limit(200)
        if status:
            q = q.where(Job.status == status)
        jobs = s.scalars(q).all()
    return templates.TemplateResponse(request, "jobs.html", {
        "request": request, "jobs": jobs, "status_filter": status})


class WebJobForm(BaseModel):
    meeting_link: str
    topic: str | None = None
    start_at: str | None = None  # ISO ili pustoy=dlya necentral'nogo


@app.post("/jobs/create")
async def web_job_create(request: Request):
    form = await request.form()
    link = form.get("meeting_link", "")
    topic = form.get("topic", "")
    start_raw = form.get("start_at", "")
    start_at = None
    if start_raw:
        try:
            start_at = dt.datetime.fromisoformat(start_raw)
        except ValueError:
            raise HTTPException(400, "NEKORREKTNAYA DATA")
    job = create_job(meeting_link=link, topic=topic, start_at=start_at, created_by="web")
    return HTMLResponse(
        f"<script>window.location='/jobs/{job.id}'</script>"
        if request.headers.get("hx-request") else f"Job {job.id} sozdan")


@app.post("/jobs/{jid}/cancel")
async def web_job_cancel(jid: int, request: Request):
    cancel_job(jid)
    return HTMLResponse("ok")


@app.post("/jobs/import-ics")
async def web_import_ics(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8", errors="replace")
    n = import_ics(text)
    return {"imported": n}


@app.get("/jobs/{jid}", response_class=HTMLResponse)
def web_job_detail(request: Request, jid: int):
    with get_session() as s:
        job = s.get(Job, jid)
        if job is None:
            raise HTTPException(404)
        events = s.scalars(select(SessionEvent).where(
            SessionEvent.job_id == jid).order_by(SessionEvent.ts.desc()).limit(300)).all()
        audio = s.scalars(select(AudioFile).where(AudioFile.job_id == jid)).all()
        proto = s.scalars(select(Protocol).where(Protocol.job_id == jid)).first()
        reviews = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == jid)).all()
    pwd_present = job.pwd_ref is not None
    return templates.TemplateResponse(request, "job_detail.html", {
        "request": request, "job": job, "events": events, "audio": audio,
        "protocol": proto, "reviews": reviews, "pwd_present": pwd_present})


@app.get("/protocols", response_class=HTMLResponse)
def web_protocols(request: Request):
    with get_session() as s:
        protos = s.scalars(select(Protocol).order_by(Protocol.created_ts.desc()).limit(200)).all()
        jobs = {j.id: j for j in s.scalars(select(Job)).all()}
    return templates.TemplateResponse(request, "protocols.html", {
        "request": request, "protocols": protos, "jobs": jobs})


@app.get("/protocols/{pid}", response_class=HTMLResponse)
def web_protocol(request: Request, pid: int):
    with get_session() as s:
        p = s.get(Protocol, pid)
        if p is None:
            raise HTTPException(404)
        job = s.get(Job, p.job_id)
        segs = s.scalars(select(Segment).where(
            Segment.job_id == p.job_id).order_by(Segment.t_start)).all()
    md = ""
    if p.md_path and Path(p.md_path).exists():
        md = Path(p.md_path).read_text(encoding="utf-8")
    return templates.TemplateResponse(request, "protocol_detail.html", {
        "request": request, "protocol": p, "job": job, "segments": segs, "md": md})


@app.get("/reviews", response_class=HTMLResponse)
def web_reviews(request: Request):
    with get_session() as s:
        pending = s.scalars(select(ReviewRequest).where(
            ReviewRequest.status == "pending").order_by(ReviewRequest.id.desc())).all()
        profiles = s.scalars(select(VoiceProfile)).all()
        jobs = {j.id: j for j in s.scalars(select(Job)).all()}
        # etalonnie fragmenty: po segmentu na klastery
        refsegs = {}
        for r in pending:
            seg = s.scalars(select(Segment).where(
                Segment.job_id == r.job_id,
                Segment.speaker_id == r.cluster_label).limit(5))
            refsegs[r.id] = list(seg)
    return templates.TemplateResponse(request, "reviews.html", {
        "request": request, "pending": pending, "profiles": profiles,
        "jobs": jobs, "refsegs": refsegs})


@app.get("/voices", response_class=HTMLResponse)
def web_voices(request: Request):
    with get_session() as s:
        profiles = s.scalars(select(VoiceProfile)).all()
    return templates.TemplateResponse(request, "voices.html", {
        "request": request, "profiles": profiles})


def main() -> None:
    import uvicorn
    errors = registry.preflight()
    if errors:
        for e in errors:
            print(f"PREFLIGHT: {e}", file=__import__("sys").stderr, flush=True)
    uvicorn.run("mnema.api.main:app", host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
