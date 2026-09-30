"""Mnema api: REST (FT-21) + vec-interfeys (FT-22) + APScheduler (6.4).

Zapusk: python -m mnema.api.main
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select

from ..core import registry
from ..core.capabilities import capability_status
from ..core.db import Base, get_engine, get_session
from ..core.jobs_api import cancel_job, create_job, delete_job, rename_job
from ..core.models import (AudioFile, Job, Notification, PresetLink, Protocol,
                           ReviewRequest, Segment, SessionEvent, SessionHealth,
                           Upload, VoiceProfile)
from ..core.notify import NotifyService
from ..core.pipeline import review_requests_resolved, transition
from ..core.presets_api import add_preset, delete_preset, get_preset_join, list_presets
from ..core.scheduler import import_ics, start_default_jobs
from ..core.secrets_store import get_pwd
from ..core.zoom_link import link_valid
from ..core import uploads as uploads_svc
from ..core import vnc as vnc_svc
from ..core.tz import app_tz_label, fmt_local, local_to_utc, local_utc_offset, to_local

app = FastAPI(title="Mnema", version="0.1.0")
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def human_size(n):
    if not n:
        return "?"
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.1f} {unit}".replace(".0 ", " ")
        size /= 1024


def human_duration(seconds):
    if seconds is None:
        return "?"
    total = int(round(float(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m} мин {s} с"
    if m:
        return f"{m} мин {s} с"
    return f"{s} с"


templates.env.filters["msk"] = lambda v: fmt_local(v)
templates.env.filters["human_duration"] = human_duration
templates.env.filters["msk_sec"] = lambda v: fmt_local(v, "%d.%m.%Y %H:%M:%S")
templates.env.filters["human_size"] = human_size


def stats_text(stats: dict | None) -> str:
    """Match_stats profilya v cheloveko-chitayemom vide (vmesto syrovogo dict)."""
    if not stats:
        return "—"
    labels = [("confirmed", "подтверждено"), ("auto_confirmed", "авто"),
              ("enrollments", "enroll"), ("backfill_clusters", "кластеров в эталоне")]
    parts = [f"{lbl}: {stats[key]}" for key, lbl in labels if stats.get(key)]
    for ev in ("reverify_done", "reverify_failed", "profile_reembed"):
        info = stats.get(ev)
        if isinstance(info, dict) and info.get("ts"):
            mark = "oshibka pereproverki" if ev == "reverify_failed" else ev
            parts.append(f"{mark}: {info['ts'][:16].replace('T', ' ')} ({info.get('detail', '')})")
    return "; ".join(parts) or "—"


templates.env.globals["stats_text"] = stats_text
templates.env.globals["app_tz_label"] = app_tz_label
templates.env.globals["local_utc_offset"] = local_utc_offset
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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
    try:
        from ..core.scheduler import reschedule_missed_jobs
        n = reschedule_missed_jobs()
        if n:
            print(f"rescheduled missed jobs: {n}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"reschedule missed jobs failed: {e}", flush=True)


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
                "join_link": job.join_link, "end_at": job.end_at}


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
    from ..core.queue import enqueue_job
    processing_started = enqueue_job("asr", "mnema.worker.tasks.run_orchestrate", jid)
    if not processing_started:
        pass  # worker vzjanet sam po ocheredi statusov; v dev — run inline
    return {"ok": True}


@app.post("/api/jobs/{jid}/leave")
def api_job_leave(jid: int):
    """Ruchnoe zavershenie vstrechi: web-bot vyhodit iz sozwona (knopka v WEB).
    Zapis' korrectno zavershaetsya i uhoit v processing (recording) /
    otfmenyaetsya (launching, esli bot eshche ne vshe)."""
    from ..core.jobs_launch import request_stop
    with get_session() as s:
        job = s.get(Job, jid)
        if job is None:
            raise HTTPException(404)
        if job.status not in ("launching", "recording"):
            raise HTTPException(409, f"vyhod nemozhno dlya statusa: {job.status}")
        s.add(SessionEvent(job_id=jid, event="leave_requested",
                           detail="ruchnoe zavershenie operatorom"))
        s.commit()
        status = job.status
    stop_queued = request_stop(jid, reason="manual_leave")
    return {"ok": True, "status": status, "stop_queued": stop_queued}


# ------------------------------------------------------------------- VNC (FT-24/§12)
def _vnc_url(request: Request, port: int) -> str:
    host = (request.url.hostname if request else None) or "localhost"
    return f"http://{host}:{port}/vnc.html"


VNC_LIVE_STATUSES = ("ready", "running")


def _vnc_payload(request: Request, sess: dict | None) -> str | None:
    """Ssjylka noVNC dlya zhivoy sessii (inic — None). Eddinstvennyy tochka
    postroeniya ssylki: statusy i public_port chitayutsya zdes', ne v shablonah."""
    if not sess or sess.get("status") not in VNC_LIVE_STATUSES:
        return None
    return _vnc_url(request, sess.get("public_port", vnc_svc.public_port_base()))


@app.get("/api/vnc/sessions")
def api_vnc_sessions():
    """Sostoyaniye vseh VNC-sessiy (setup + job-<id>) — dlya UI i diagnostiki."""
    return vnc_svc.read_sessions()


@app.post("/api/vnc/setup/open")
def api_vnc_setup_open(request: Request):
    """Sessiya nastroyki: otkryt'/podnyat' zhivoy brauzer s Zoom dlya vyezda
    operatora (pechenki i otpechatki bazy). Vozvrassaet ssylku na noVNC."""
    queued = vnc_svc.request_setup_open()
    s = vnc_svc.get_setup()
    return {"ok": True, "queued": queued, "session": s, "url": _vnc_payload(request, s)}


@app.post("/api/vnc/setup/restart")
def api_vnc_setup_restart(request: Request):
    queued = vnc_svc.request_setup_restart()
    s = vnc_svc.get_setup()
    return {"ok": True, "queued": queued, "session": s, "url": _vnc_payload(request, s)}


@app.post("/api/jobs/{jid}/vnc")
def api_job_vnc(jid: int, request: Request):
    """Ekran vstrechi: esli VNC-sessiya zhivaya (ready/running) — srazu
    vozvrashchaet ssylku; inache stavit komandu sozdaniya i pishet sobytie.
    URL tayetsya tol'ko dlya zhivoy sessii — na myortvyy pot UI ne vedём."""
    with get_session() as s:
        job = s.get(Job, jid)
        if job is None:
            raise HTTPException(404)
        if job.source == "upload":
            raise HTTPException(409, "dlya arhivnoy zapisi VNC ne nuzhen")
        live = vnc_svc.get_for_job(jid)
        if not live or live.get("status") not in VNC_LIVE_STATUSES:
            s.add(SessionEvent(job_id=jid, event="vnc_requested",
                               detail="individual'nyy ekran vstrechi"))
            s.commit()
    url = _vnc_payload(request, live)
    if url:
        return {"ok": True, "queued": False, "session": live, "url": url}
    queued = vnc_svc.request_open(jid)
    return JSONResponse({"ok": True, "queued": queued, "session": live,
                         "url": None}, status_code=202)


@app.delete("/api/jobs/{jid}/vnc")
def api_job_vnc_close(jid: int):
    queued = vnc_svc.request_close(jid)
    return {"ok": True, "queued": queued}


@app.get("/api/protocols/{pid}")
def api_protocol(pid: int, format: str = "md"):
    from ..core.protocol_md import refresh_protocol
    if format in ("md", "docx"):
        refresh_protocol(pid)  # imena mogut utochnitsya posle publikacii
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


def _serve_path(path: Path, mime: str, request: Request | None):
    size = path.stat().st_size
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


ORIG_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv",
    ".webm": "video/webm", ".flv": "video/x-flv", ".ts": "video/mp2t",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".3gp": "video/3gpp",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".opus": "audio/opus",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".flac": "audio/flac",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".amr": "audio/amr",
}


def _serve_audio(job_id: int, fmt: str, request: Request | None):
    if fmt not in ("mp3", "wav", "webm"):
        raise HTTPException(400, "format: mp3|wav|webm")
    with get_session() as s:
        a = s.scalars(select(AudioFile).where(
            AudioFile.job_id == job_id, AudioFile.kind == fmt)).first()
    if a is None or not Path(a.path).exists():
        raise HTTPException(404, f"audio {fmt} otsutstvuet")
    path = Path(a.path)
    mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "webm": "audio/webm"}[fmt]
    return _serve_path(path, mime, request)


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
    if updated:
        from ..core.protocol_md import refresh_protocol
        refresh_protocol(pid)  # imena v vygruzkah dolzhnybyt' aktual'nymi
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
    _enqueue_voice_enroll(job_id, cluster)
    from ..core.pipeline import resume_after_reviews
    done_now = resume_after_reviews(job_id)
    return {"ok": True, "name": name, "job_done": done_now}


def _enqueue_voice_enroll(job_id: int, cluster_label: str) -> None:
    """Reestr golosa: utocnenie operatora uluchshaet etalon — pereschitat'
    embedding profila v workere (tam pyannote). V direct mode (testy/bez
    redis) — noop: v api zhivyh embeddingov ne byvaet."""
    from ..core.queue import enqueue_job
    try:
        enqueue_job("speaker", "mnema.worker.tasks.run_voice_enroll",
                    job_id, cluster_label)
    except Exception:  # noqa: BLE001 — enroll ne lonit otvet operatora
        pass


@app.delete("/api/reviews/{rid}/segments/{seg_id}")
def api_exclude_review_segment(rid: int, seg_id: int):
    """Udalit' fragment iz klastera: segment perenositsya v sobstvennyy
    podklaster i poluchaet shtuknyun pending review-request."""
    from ..core.pipeline import resume_after_reviews
    with get_session() as s:
        r = s.get(ReviewRequest, rid)
        seg = s.get(Segment, seg_id)
        if r is None or seg is None or seg.job_id != r.job_id or seg.speaker_id != r.cluster_label:
            raise HTTPException(404)
        if r.status != "pending":
            raise HTTPException(409, "review uzhe otvechen")
        new_label = f"{r.cluster_label}-x{seg.id}"
        seg.speaker_id = new_label
        seg.speaker_name = None
        seg.speaker_source = "unresolved"
        seg.needs_review = True
        s.add(SessionEvent(job_id=r.job_id, event="segment_detached",
                           detail=f"seg {seg.id} -> {new_label}"))
        left = s.scalars(select(Segment).where(
            Segment.job_id == r.job_id,
            Segment.speaker_id == r.cluster_label,
            Segment.excluded.is_(False))).first()
        review_closed = False
        if left is None:
            r.status = "superseded"
            r.answered_by = "web"
            r.answered_ts = dt.datetime.now(dt.timezone.utc)
            review_closed = True
        dup = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == r.job_id,
            ReviewRequest.cluster_label == new_label,
            ReviewRequest.status == "pending")).first()
        if dup is None:
            s.add(ReviewRequest(job_id=r.job_id, cluster_label=new_label))
        job_id = r.job_id
        s.commit()
    done_now = resume_after_reviews(job_id) if review_closed else False
    return {"ok": True, "cluster_label": new_label, "review_closed": review_closed,
            "job_done": done_now}


@app.post("/api/reviews/{rid}/segments/{seg_id}/exclude")
def api_exclude_segment_from_analysis(rid: int, seg_id: int):
    """Isklyuchit' fragment iz analiza polnost'yu (dva golosa/postoronnaya
    zvuki): segment otmechaetsya excluded i ne popadaet v steno/protokol.
    Esli v klastere ne ostalos' neisklyuchennykh fragmentov — review-request
    zakryvaetsya i analiz vozobnovlyaetsya."""
    from ..core.pipeline import resume_after_reviews
    with get_session() as s:
        r = s.get(ReviewRequest, rid)
        seg = s.get(Segment, seg_id)
        if r is None or seg is None or seg.job_id != r.job_id or seg.speaker_id != r.cluster_label:
            raise HTTPException(404)
        if r.status != "pending":
            raise HTTPException(409, "review uzhe otvechen")
        seg.excluded = True
        seg.needs_review = False
        seg.speaker_source = "excluded"
        s.add(SessionEvent(job_id=r.job_id, event="segment_excluded",
                           detail=f"seg {seg.id} (klaster {r.cluster_label})"))
        remaining = s.scalars(select(Segment).where(
            Segment.job_id == r.job_id,
            Segment.speaker_id == r.cluster_label,
            Segment.excluded.is_(False))).first()
        cluster_closed = remaining is None
        if cluster_closed:
            r.status = "excluded"
            r.answered_by = "web"
            r.answered_ts = dt.datetime.now(dt.timezone.utc)
        job_id = r.job_id
        s.commit()
    done_now = resume_after_reviews(job_id) if cluster_closed else False
    return {"ok": True, "cluster_closed": cluster_closed, "job_done": done_now}


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
    """FT-29a: zapis' s mikrofona (MediaRecorder webm). Embedding schitaet
    WORKER (tam pyannote + GPU): v api-bez-ASR ranshe zapisyvalsya 32-D
    hash-zaglushka, kotoryy bezvoz vratno portil profil' (cosine k 512-D
    etalonam ~0). Esli API sobran s ASR — schitaem inline, inache v ochered'.

    Udalennyy rezhim kacheira: kachestvo proveryaetsya zdes' (ffmpeg est'
    v oboih obraze), embedding — v workere."""
    with get_session() as s:
        if s.get(VoiceProfile, vid) is None:
            raise HTTPException(404)
    dest_dir = Path(os.environ.get("DATA_DIR", "/data")) / "voice_samples" / str(vid)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"sample_{dt.datetime.now(dt.timezone.utc):%Y%m%d_%H%M%S}.webm"
    dest.write_bytes(await file.read())
    from ..core.speaker import _embedding_backend, compute_embedding_for_file, quality_gate
    ok, reason = quality_gate(dest)
    if not ok:
        dest.unlink(missing_ok=True)
        return {"ok": False, "reason": reason}
    if _embedding_backend() == "pyannote":
        emb = compute_embedding_for_file(dest)
        if not emb:
            return {"ok": False,
                    "reason": "ne udalos' postroit' embedding (srabrable?)"}
        with get_session() as s:
            v = s.get(VoiceProfile, vid)
            if v is None:
                raise HTTPException(404)
            v.sample_paths = list(v.sample_paths or []) + [str(dest)]
            _append_sample_ref(s, v, emb)
            s.commit()
        return {"ok": True}
    # api bez pyannote: etalon dobazhaetsya (DATA_DIR — obshiy volume s worker),
    # embedding postavit worker
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        if v is None:
            raise HTTPException(404)
        v.sample_paths = list(v.sample_paths or []) + [str(dest)]
        s.commit()
    from ..core.queue import enqueue_job
    enqueued = enqueue_job("speaker", "mnema.worker.tasks.run_profile_sample_enroll",
                           vid, str(dest)) is not None
    return {"ok": True, "pending_embedding": True, "worker_queued": enqueued}


def _append_sample_ref(s, v: VoiceProfile, emb: list[float]) -> None:
    """Dobavit' etalon zapisi v set' ref_embeddings (idempotentnost' po puti)."""
    from ..core.speaker_backfill import append_ref
    append_ref(v, emb, f"path:{v.sample_paths[-1] if v.sample_paths else 'ref'}")


SAMPLE_MIME = {".webm": "audio/webm", ".wav": "audio/wav", ".mp3": "audio/mpeg",
               ".ogg": "audio/ogg", ".m4a": "audio/mp4"}


def _profile_or_404(vid: int):
    with get_session() as s:
        return s.get(VoiceProfile, vid)


def _profile_sample_paths(vid: int) -> list[str]:
    v = _profile_or_404(vid)
    if v is None:
        raise HTTPException(404)
    return list(v.sample_paths or [])


@app.get("/api/voice-profiles/{vid}/samples")
def api_profile_samples(vid: int):
    """Spisok etalonov profilya: kazhdyiy dostupen dlya proslushivaniya po url."""
    out = []
    for i, p in enumerate(_profile_sample_paths(vid)):
        path = Path(p)
        out.append({"index": i, "name": path.name, "exists": path.is_file(),
                    "size": path.stat().st_size if path.is_file() else None,
                    "url": f"/api/voice-profiles/{vid}/samples/{i}"})
    return out


@app.get("/api/voice-profiles/{vid}/samples/{idx}")
def api_profile_sample_file(vid: int, idx: int, request: Request = None):
    """Proslushivat' / skalchat' odin etalon (Range podderzhivaetsya)."""
    paths = _profile_sample_paths(vid)
    if idx < 0 or idx >= len(paths) or not Path(paths[idx]).is_file():
        raise HTTPException(404, "etalon otsutstvuet")
    path = Path(paths[idx])
    return _serve_path(path, SAMPLE_MIME.get(path.suffix.lower(),
                                             "application/octet-stream"), request)


@app.delete("/api/voice-profiles/{vid}/samples/{idx}")
def api_delete_profile_sample(vid: int, idx: int):
    """Udalit' odin etalon; embedding profila pereschitaetsya v workere po
    ostavshimsya etalonam/klasteryam."""
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        if v is None:
            raise HTTPException(404)
        paths = list(v.sample_paths or [])
        if idx < 0 or idx >= len(paths):
            raise HTTPException(404, "etalon otsutstvuet")
        removed = paths.pop(idx)
        v.sample_paths = paths
        s.commit()
    try:
        Path(removed).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        from ..core.queue import enqueue_job
        enqueue_job("speaker", "mnema.worker.tasks.run_profile_reembed", vid)
    except Exception:  # noqa: BLE001 — udalenie ne lonitsya ochered'yu
        pass
    return {"ok": True, "samples": len(paths)}


class ProfilePatch(BaseModel):
    employee_name: str | None = None
    employee_id: str | None = None


@app.patch("/api/voice-profiles/{vid}")
def api_patch_profile(vid: int, body: ProfilePatch):
    """Pravka reestra: FIO/tabernoy; peremeynovanie rezonya v segmentakh
    podtverzhdennykh klasteov + osvezhenie vygruzhnykh protokolov."""
    if body.employee_name is not None and not body.employee_name.strip():
        raise HTTPException(400, "FIO ne mozhet byt' pustym")
    touched_protocol_ids: list[int] = []
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        if v is None:
            raise HTTPException(404)
        old_name = v.employee_name
        if body.employee_name is not None:
            v.employee_name = body.employee_name.strip()
        if body.employee_id is not None:
            v.employee_id = body.employee_id.strip() or None
        name = v.employee_name
        if v.employee_name != old_name:
            reviews = s.scalars(select(ReviewRequest).where(
                ReviewRequest.candidate_profile_id == vid,
                ReviewRequest.status == "confirmed")).all()
            job_ids = {r.job_id for r in reviews}
            for r in reviews:
                for seg in s.scalars(select(Segment).where(
                        Segment.job_id == r.job_id,
                        Segment.speaker_id == r.cluster_label,
                        Segment.speaker_name == old_name)).all():
                    seg.speaker_name = name
                    seg.speaker_source = "voiceprint"
            if job_ids:
                touched_protocol_ids = s.scalars(select(Protocol.id).where(
                    Protocol.job_id.in_(job_ids))).all()
        s.commit()
    if touched_protocol_ids:
        try:
            from ..core.protocol_md import refresh_protocol
            for pid in touched_protocol_ids:
                refresh_protocol(pid)
        except Exception:  # noqa: BLE001 — vygruzki ne kritichny dlya pravki
            pass
    return {"ok": True, "employee_name": name}


@app.get("/api/voice-profiles/{vid}/clusters")
def api_profile_clusters(vid: int):
    """Klastry cheloveka (podtverzhdennye + zhdushchie utochneniya) s fragmentami
    dlya proslushivaniya: audio + #t= (kak v /reviews)."""
    with get_session() as s:
        if s.get(VoiceProfile, vid) is None:
            raise HTTPException(404)
        rows = s.scalars(select(ReviewRequest).where(
            ReviewRequest.candidate_profile_id == vid,
            ReviewRequest.status.in_(("confirmed", "pending")))
            .order_by(ReviewRequest.id.desc())).all()
        out = []
        for r in rows:
            job = s.get(Job, r.job_id)
            files = s.scalars(select(AudioFile).where(
                AudioFile.job_id == r.job_id,
                AudioFile.kind.in_(("wav", "mp3")))).all()
            by_kind = {f.kind: f for f in files if Path(f.path).exists()}
            picked = by_kind.get("wav") or by_kind.get("mp3")
            segs = s.scalars(select(Segment).where(
                Segment.job_id == r.job_id,
                Segment.speaker_id == r.cluster_label,
                Segment.excluded.is_(False)).order_by(Segment.t_start)).all()
            out.append({"review_id": r.id, "job_id": r.job_id,
                        "topic": job.topic if job else "",
                        "cluster_label": r.cluster_label, "status": r.status,
                        "answered_by": r.answered_by, "similarity": r.similarity,
                        "has_audio": picked is not None,
                        "audio_format": picked.kind if picked else None,
                        "segments": [{"t_start": round(seg.t_start, 2),
                                      "t_end": round(seg.t_end, 2),
                                      "text": (seg.text or "")[:160]}
                                     for seg in segs]})
        return out


@app.post("/api/voice-profiles/{vid}/reverify")
def api_reverify_profile(vid: int):
    """Ruchnoy перезапуск raspoznavaniya dlya cheloveka: peresobrat' etalon iz
    podtverzhdennykh klasteov i zanovo opoznat' vse egо vstrechi (vrabotaet
    v workere, tam pyannote)."""
    if _profile_or_404(vid) is None:
        raise HTTPException(404)
    from ..core.queue import enqueue_job
    try:
        rq = enqueue_job("speaker", "mnema.worker.tasks.run_reverify_profile", vid)
    except Exception:  # noqa: BLE001 — esli redis lez, otkazyvaem s Chestnо otvetom
        rq = None
    return {"ok": True, "queued": rq is not None}


# ------------------------------------------------- upload arhivnyh zapisey (FT-upload)
class UploadInit(BaseModel):
    filename: str
    bytes_total: int | None = None
    job_id: int | None = None
    topic: str | None = None
    meeting_date: dt.datetime | None = None
    note: str | None = None


@app.post("/api/uploads")
def api_upload_init(body: UploadInit):
    try:
        job, up = uploads_svc.create_upload(
            filename=body.filename, bytes_total=body.bytes_total,
            job_id=body.job_id, topic=body.topic or "",
            meeting_date=body.meeting_date, note=body.note)
    except (OverflowError,) as e:
        raise HTTPException(413, str(e))
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"job_id": job.id, "upload_id": up.id,
            "offset": up.bytes_received, "chunk_mb": uploads_svc.chunk_mb()}


@app.get("/api/uploads/{uid}/offset")
def api_upload_offset(uid: int):
    try:
        off, status = uploads_svc.offset_of(uid)
    except KeyError:
        raise HTTPException(404)
    return {"offset": off, "status": status}


@app.post("/api/uploads/{uid}/chunk")
async def api_upload_chunk(uid: int, request: Request, offset: int | None = None):
    data = await request.body()
    try:
        up = uploads_svc.append_chunk(uid, data, offset=offset)
    except (OverflowError,) as e:
        raise HTTPException(413, str(e))
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"offset": up.bytes_received, "status": up.status}


@app.post("/api/uploads/{uid}/finalize")
def api_upload_finalize(uid: int):
    try:
        job = uploads_svc.finalize(uid)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "job_id": job.id, "status": job.status}


@app.post("/api/uploads/{uid}/cancel")
def api_upload_cancel(uid: int):
    try:
        job = uploads_svc.cancel_upload(uid)
    except KeyError:
        raise HTTPException(404)
    return {"ok": True, "job_id": job.id, "status": job.status if job else None}


@app.get("/api/jobs/{jid}/uploads/active")
def api_upload_active(jid: int):
    """Akturnaya (nedokonchennaya) zagruzka vstrechi dlya vozobnovleniya."""
    with get_session() as s:
        if s.get(Job, jid) is None:
            raise HTTPException(404)
        up = s.scalars(select(Upload).where(
            Upload.job_id == jid,
            Upload.status.in_(("pending", "uploading")))
            .order_by(Upload.id.desc())).first()
    if up is None:
        return {"upload_id": None}
    real = Path(up.path).stat().st_size if Path(up.path).exists() else 0
    return {"upload_id": up.id, "filename": up.filename,
            "bytes_total": up.bytes_total,
            "offset": min(up.bytes_received, real), "status": up.status}


@app.get("/api/jobs/{jid}/source")
def api_job_source(jid: int, request: Request = None):
    """Razdacha ishodnogo arhivnogo video/audio (Range) dlya ob'yav source=upload."""
    with get_session() as s:
        if s.get(Job, jid) is None:
            raise HTTPException(404)
        up = s.scalars(select(Upload).where(
            Upload.job_id == jid, Upload.status == "complete")
            .order_by(Upload.id.desc())).first()
    if up is None or not Path(up.path).exists():
        raise HTTPException(404, "ishodnyy fayl otsutstvuet")
    path = Path(up.path)
    mime = ORIG_MIME.get(path.suffix.lower(), "application/octet-stream")
    return _serve_path(path, mime, request)


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
            Job.status.in_(("scheduled", "launching", "recording", "processing",
                            "uploading"))
        ).order_by(Job.id).limit(20)).all()
        recent_failed = s.scalars(select(Job).where(
            Job.status == "failed").order_by(Job.ended_ts.desc()).limit(10)).all()
        h = s.scalars(select(SessionHealth).order_by(SessionHealth.checked_ts.desc())).first()
    queue_ok = _queue_ok()
    setup = vnc_svc.get_setup()
    if setup:
        setup["url"] = _vnc_payload(request, setup)
    vnc_jobs: dict[int, dict] = {}
    for key, sess in vnc_svc.read_sessions().items():
        if key.startswith("job-") and sess.get("job_id") is not None:
            sess["url"] = _vnc_payload(request, sess)
            vnc_jobs[int(sess["job_id"])] = sess
    from ..core.stages import running_stage_title
    job_stage = {}
    for j in active:
        if j.status == "processing":
            t = running_stage_title(j.id)
            if t:
                job_stage[j.id] = t
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "caps": caps, "active": active,
        "recent_failed": recent_failed, "zoom_health": h, "queue_ok": queue_ok,
        "vnc_setup": setup, "vnc_jobs": vnc_jobs, "job_stage": job_stage,
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
def web_jobs(request: Request, status: str | None = None, error: str | None = None):
    with get_session() as s:
        q = select(Job).order_by(Job.id.desc()).limit(200)
        if status:
            q = q.where(Job.status == status)
        jobs = s.scalars(q).all()
    error_text = {"link": "Некорректная ссылка Zoom: нужна ссылка вида "
                          "https://us06web.zoom.us/j/123456789?pwd=... или ID встречи (9–11 цифр)",
                  "date": "Некорректная дата начала",
                  "preset": "Предопределённая ссылка не найдена",
                  "end_before_start": "Время окончания должно быть позже времени начала"}.get(error or "", None)
    return templates.TemplateResponse(request, "jobs.html", {
        "request": request, "jobs": jobs, "status_filter": status,
        "presets": list_presets(),
        "error_text": error_text})


@app.post("/presets/add")
async def web_preset_add(request: Request):
    form = await request.form()
    try:
        add_preset(title=str(form.get("title", "")),
                   meeting_link=str(form.get("meeting_link", "")),
                   pwd=str(form.get("pwd", "")).strip() or None)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return RedirectResponse("/jobs", status_code=303)


@app.post("/presets/{pid}/delete")
async def web_preset_delete(pid: int):
    if not delete_preset(pid):
        raise HTTPException(404)
    return HTMLResponse("ok")


class WebJobForm(BaseModel):
    meeting_link: str
    topic: str | None = None
    start_at: str | None = None  # ISO ili pustoy=dlya necentral'nogo


@app.post("/jobs/create")
async def web_job_create(request: Request):
    form = await request.form()
    preset_id = str(form.get("preset_id", "")).strip()
    link = str(form.get("meeting_link", "")).strip()
    pwd: str | None = None
    if preset_id:
        # applyPreset() заполняет meeting_link ссылкой пресета (readonly), а
        # пароль в полях формы нет: пресет важнее скопированной ссылки.
        # Условие "and not link" раньше его пропускало -> job без pwd_ref,
        # бот упирался в экран пинкода (vstrecha 56).
        try:
            preset = get_preset_join(int(preset_id))
        except ValueError:
            return RedirectResponse("/jobs?error=preset", status_code=303)
        if preset is None:
            return RedirectResponse("/jobs?error=preset", status_code=303)
        link, pwd = preset
    topic = str(form.get("topic", "")).strip()
    start_raw = form.get("start_at", "")
    start_now = str(form.get("start_now", "")).lower() in ("1", "true", "on") \
        or form.get("mode") == "start_now"
    if not link_valid(link):
        return RedirectResponse("/jobs?error=link", status_code=303)
    start_at = None
    if start_raw and not start_now:
        try:
            start_at = dt.datetime.fromisoformat(start_raw)
        except ValueError:
            return RedirectResponse("/jobs?error=date", status_code=303)
        start_at = local_to_utc(start_at)  # datetime-local = время оператора (МСК)
    end_raw = str(form.get("end_at", "")).strip()
    end_at = None
    if end_raw:
        try:
            end_at = dt.datetime.fromisoformat(end_raw)
        except ValueError:
            return RedirectResponse("/jobs?error=date", status_code=303)
        end_at = local_to_utc(end_at)
    if end_at is not None and start_at is not None and end_at <= start_at:
        return RedirectResponse("/jobs?error=end_before_start", status_code=303)
    try:
        job = create_job(meeting_link=link, pwd=pwd, topic=topic, start_at=start_at,
                         end_at=end_at, created_by="web")
    except ValueError:
        return RedirectResponse("/jobs?error=end_before_start", status_code=303)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.post("/jobs/{jid}/cancel")
async def web_job_cancel(jid: int, request: Request):
    cancel_job(jid)
    return HTMLResponse("ok")


@app.post("/jobs/{jid}/rename")
async def web_job_rename(jid: int, request: Request):
    form = await request.form()
    topic = str(form.get("topic", "")).strip()
    if not topic:
        raise HTTPException(422, "Nazvanie ne mozhet byt' pustym")
    job = rename_job(jid, topic)
    if job is None:
        raise HTTPException(404)
    return HTMLResponse(job.topic)


@app.post("/jobs/{jid}/delete")
async def web_job_delete(jid: int):
    with get_session() as s:
        job = s.get(Job, jid)
        if job is None:
            raise HTTPException(404)
        if job.status in ("scheduled", "launching", "recording"):
            raise HTTPException(409, "Ostanovite vstrechu pered udaleniem")
    delete_job(jid)
    return RedirectResponse("/jobs", status_code=303)


@app.post("/jobs/{jid}/leave")
async def web_job_leave(jid: int):
    r = api_job_leave(jid)
    return JSONResponse(r)


@app.post("/jobs/{jid}/vnc")
async def web_job_vnc(jid: int, request: Request):
    return api_job_vnc(jid, request)


@app.post("/jobs/{jid}/rerun-llm")
def web_job_rerun_llm(jid: int):
    """Ruchnoy perezapusk LLM-analiza posle utochneniya spikero (knopka v WEB)."""
    from ..core.pipeline import rerun_llm_analysis
    try:
        started = rerun_llm_analysis(jid)
    except KeyError:
        raise HTTPException(404)
    if not started:
        raise HTTPException(409, "perezapusk dopustim tol'ko dlya done/awaiting_llm")
    return {"ok": True, "status": "processing"}


@app.post("/jobs/import-ics")
async def web_import_ics(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8", errors="replace")
    n = import_ics(text)
    return {"imported": n}


@app.get("/jobs/upload", response_class=HTMLResponse)
def web_upload_page(request: Request, job_id: int | None = None):
    return templates.TemplateResponse(request, "upload.html", {
        "request": request, "chunk_mb": uploads_svc.chunk_mb(),
        "upload_job_id": job_id})


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
            ReviewRequest.job_id == jid,
            ReviewRequest.status == "pending")).all()
        up = s.scalars(select(Upload).where(
            Upload.job_id == jid).order_by(Upload.id.desc()).limit(1)).first()
    pwd_present = job.pwd_ref is not None
    vsess = vnc_svc.get_for_job(jid)
    if vsess:
        vsess["url"] = _vnc_payload(request, vsess)
    from ..core.stages import build_stages
    return templates.TemplateResponse(request, "job_detail.html", {
        "request": request, "job": job, "events": events, "audio": audio,
        "protocol": proto, "reviews": reviews, "pwd_present": pwd_present,
        "upload": up, "stages": build_stages(jid), "vnc_session": vsess})


@app.get("/jobs/{jid}/log", response_class=HTMLResponse)
def web_job_log(request: Request, jid: int):
    from ..core.stages import build_stages
    with get_session() as s:
        job = s.get(Job, jid)
        if job is None:
            raise HTTPException(404)
        events = s.scalars(select(SessionEvent).where(
            SessionEvent.job_id == jid).order_by(SessionEvent.ts.desc()).limit(300)).all()
    return templates.TemplateResponse(request, "job_log.html", {
        "request": request, "job": job, "events": events,
        "stages": build_stages(jid)})


@app.get("/jobs/{jid}/stages", response_class=HTMLResponse)
def web_job_stages(request: Request, jid: int):
    from ..core.stages import build_stages
    with get_session() as s:
        job = s.get(Job, jid)
        if job is None:
            raise HTTPException(404)
    return templates.TemplateResponse(request, "job_stages.html", {
        "request": request, "job": job, "stages": build_stages(jid)})


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
            Segment.job_id == p.job_id, Segment.excluded.is_(False)
        ).order_by(Segment.t_start)).all()
    md = ""
    if p.md_path and Path(p.md_path).exists():
        md = Path(p.md_path).read_text(encoding="utf-8")
    return templates.TemplateResponse(request, "protocol_detail.html", {
        "request": request, "protocol": p, "job": job, "segments": segs, "md": md})


@app.get("/reviews", response_class=HTMLResponse)
def web_reviews(request: Request, job_id: int | None = None):
    with get_session() as s:
        crit = [ReviewRequest.status == "pending"]
        if job_id is not None:
            crit.append(ReviewRequest.job_id == job_id)
        pending = s.scalars(select(ReviewRequest).where(*crit)
                            .order_by(ReviewRequest.id.desc())).all()
        profiles = s.scalars(select(VoiceProfile)).all()
        jobs = {j.id: j for j in s.scalars(select(Job)).all()}
        # etalonnie fragmenty: po segmentu na klastery
        refsegs = {}
        for r in pending:
            seg = s.scalars(select(Segment).where(
                Segment.job_id == r.job_id,
                Segment.speaker_id == r.cluster_label,
                Segment.excluded.is_(False))
                .order_by(Segment.t_start))
            refsegs[r.id] = list(seg)
    return templates.TemplateResponse(request, "reviews.html", {
        "request": request, "pending": pending, "profiles": profiles,
        "jobs": jobs, "refsegs": refsegs, "job_id": job_id})


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
