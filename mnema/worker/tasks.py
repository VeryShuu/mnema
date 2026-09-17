"""Zadachi konveyera (FT-34) dlya RQ-workera (roli: asr/speaker/structure/archive).

Kazhdaya zadacha idempotentna (HT-2): povtornyy zabeg peresozdayet rezultat s
nulya. Vne-zavisimosti (gigastt/ollama) — capability, FT-39.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select

from ..core.db import get_session
from ..core.models import AudioFile, Job, Protocol, Segment, SessionEvent


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def _job_paths(job_id: int) -> dict[str, Path]:
    d = _data_dir() / "jobs" / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return {"dir": d, "webm": d / "raw.webm", "wav": d / "meeting.wav",
            "mp3": d / "meeting.mp3", "steno": d / "steno.md"}


# ---------------------------------------------------------------- ASR (FT-11..15)
def run_asr_pipeline(job_id: int) -> None:
    from ..core.pipeline import transition
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
    p = _job_paths(job_id)
    try:
        _prepare_wav(job_id, p)
        with get_session() as s:
            s.add(SessionEvent(job_id=job_id, event="vad_start")); s.commit()
        from ..core.vad import segment_speech
        wav = p["wav"]
        raw_segs = segment_speech(wav) if wav.exists() else []
        with get_session() as s:
            s.add(SessionEvent(job_id=job_id, event="vad_done", detail=f"{len(raw_segs)} segm")); s.commit()
        from ..core.diarize import diarize
        labels = diarize(wav, raw_segs)
        from ..core.asr_client import transcribe_segments, engine_name
        engine = engine_name()
        results = transcribe_segments(wav, raw_segs)
        # reset segm (idempotentnost')
        with get_session() as s:
            s.query(Segment).filter(Segment.job_id == job_id).delete()
            for seg, label, txt in zip(raw_segs, labels, results):
                s.add(Segment(
                    job_id=job_id, speaker_id=label, diar_label=label,
                    raw_track="web-mix", t_start=seg["t_start"], t_end=seg["t_end"],
                    text=txt.get("text", ""), lang=txt.get("lang"),
                    engine=engine, confidence=txt.get("confidence"),
                    speaker_source="unresolved"))
            s.add(SessionEvent(job_id=job_id, event="asr_done", detail=f"engine={engine}")); s.commit()
        # speaker step (4.6)
        _run_speaker(job_id)
        # steno.md (vsegda; daze esli LLM niedostupen)
        _write_steno(job_id, p)
        # protocol (structure)
        _run_structure(job_id)
    except Exception as e:  # noqa: BLE001
        transition(job_id, "failed", fail_reason=_classify(e),
                   error=str(e)[:1000], event="asr_error")
        raise


def _classify(e: Exception) -> str:
    txt = str(e).lower()
    if "asr" in txt or "transcription" in txt:
        return "asr_error"
    return "processing_error"


def _prepare_wav(job_id: int, p: dict) -> None:
    """webm -> wav/mp3 (FT-6 + FT-41) esli eshche ne sdelano."""
    from ..core.audio import webm_to_wav16k, webm_to_mp3, repair_webm, wav_duration_s, mkvmerge_available
    webm = p["webm"]
    if not webm.exists():
        return
    if p["wav"].exists() and p["wav"].stat().st_size > 44:
        return
    if not _webm_ok(webm):
        repair_webm(webm)
    webm_to_wav16k(webm, p["wav"])
    dur = wav_duration_s(p["wav"])
    if not p["mp3"].exists():
        ok = webm_to_mp3(webm, p["mp3"]) or _wav_to_mp3(p)
        if not ok:
            # mp3 enc niedostupen — vydayem wav kak distributiv (FT-41a dopuskaet)
            pass
    _register_audio_files(job_id, p, dur)


def _wav_to_mp3(p: dict) -> bool:
    from ..core.audio import wav16k_to_mp3
    return wav16k_to_mp3(p["wav"], p["mp3"])


def _webm_ok(webm: Path) -> bool:
    from ..core.audio import ffprobe_duration
    return ffprobe_duration(webm) > 0


def _register_audio_files(job_id: int, p: dict, dur: float) -> None:
    with get_session() as s:
        for kind, path in (("wav", p["wav"]), ("mp3", p["mp3"])):
            if path.exists():
                exists = s.scalars(select(AudioFile).where(
                    AudioFile.job_id == job_id, AudioFile.kind == kind)).first()
                if exists is None:
                    s.add(AudioFile(job_id=job_id, kind=kind, path=str(path),
                                    bytes=path.stat().st_size, duration_s=dur))
        s.commit()


def run_structure(job_id: int) -> None:
    _run_structure(job_id)


def _run_speaker(job_id: int) -> None:
    """FT-25..28: emgeddingi klasteov + matsching + review zapolnenie."""
    from ..core.speaker import (compute_embedding_for_file, match_cluster,
                                thresholds)
    p = _job_paths(job_id)
    wav = p["wav"]
    with get_session() as s:
        segs = s.scalars(select(Segment).where(
            Segment.job_id == job_id).order_by(Segment.t_start)).all()
        by_cluster: dict[str, list] = {}
        for seg in segs:
            by_cluster.setdefault(seg.diar_label or "unknown", []).append(seg)
        for label, cl in by_cluster.items():
            embedding = _cluster_embedding(wav, cl)
            verdict, profile, sim = match_cluster(embedding) if embedding else ("unresolved", None, 0.0)
            t_conf, t_hint = thresholds()
            for seg in cl:
                if verdict == "auto" and profile:
                    seg.speaker_name = profile.employee_name
                    seg.speaker_source = "voiceprint"
                    seg.needs_review = False
                elif verdict == "hint" and profile:
                    seg.speaker_name = profile.employee_name
                    seg.speaker_source = "voiceprint"
                    seg.needs_review = True
                else:
                    seg.speaker_name = None
                    seg.speaker_source = "unresolved"
                    seg.needs_review = True
            # sozdat' review-request (odin na klaster)
            from ..core.models import ReviewRequest
            existing = s.scalars(select(ReviewRequest).where(
                ReviewRequest.job_id == job_id,
                ReviewRequest.cluster_label == label,
                ReviewRequest.status == "pending")).first()
            if existing is None:
                s.add(ReviewRequest(job_id=job_id, cluster_label=label,
                                    candidate_profile_id=(profile.id if verdict in ("auto", "hint") and profile else None),
                                    similarity=sim, status="pending"))
            s.commit()


def _cluster_embedding(wav: Path, segs: list) -> list[float] | None:
    from ..core.vad import extract_wav_slice
    import tempfile
    vecs = []
    for seg in segs:
        if seg.t_end - seg.t_start < 1.5:
            continue
        with tempfile.NamedTemporaryFile(suffix=".wav") as tf:
            extract_wav_slice(wav, seg.t_start, seg.t_end, Path(tf.name))
            from ..core.speaker import compute_embedding_for_file, _fallback_embedding
            vecs.append(compute_embedding_for_file(Path(tf.name)) or _fallback_embedding(Path(tf.name)))
        if len(vecs) >= 5:
            break
    if not vecs:
        return None
    n = len(vecs[0])
    return [sum(v[i] for v in vecs) / len(vecs) for i in range(n)]


def _write_steno(job_id: int, p: dict) -> Path:
    with get_session() as s:
        segs = s.scalars(select(Segment).where(
            Segment.job_id == job_id).order_by(Segment.t_start)).all()
        job = s.get(Job, job_id)
    lines = [f"# Protocol (transkript): {job.topic}", ""]
    for seg in segs:
        name = seg.speaker_name or f"Golos {seg.speaker_id}"
        tag = " (trebuetsya utochnenie)" if seg.needs_review else ""
        lines.append(f"{_fmt(seg.t_start)} {name}{tag}: {seg.text}")
    p["steno"].write_text("\n".join(lines), encoding="utf-8")
    return p["steno"]


def _fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def _run_structure(job_id: int) -> None:
    """FT-17: LLM protocol. Pri llm=not_configured/misconfigured -> awaiting_llm."""
    from ..core.capabilities import capability_status
    from ..core.pipeline import transition
    p = _job_paths(job_id)
    steno = p["steno"] if p["steno"].exists() else _write_steno(job_id, p)
    caps = capability_status()
    if caps["llm"]["state"] != "configured":
        with get_session() as s:
            proto = s.scalars(select(Protocol).where(Protocol.job_id == job_id)).first()
            if proto is None:
                proto = Protocol(job_id=job_id, json_data={"transcript_only": True},
                                 steno_md_path=str(steno))
                s.add(proto)
            else:
                proto.steno_md_path = str(steno)
            s.commit()
        transition(job_id, "awaiting_llm")
        return
    try:
        _build_protocol(job_id)
    except Exception as e:  # noqa: BLE001
        # LLm vernul neuralid Nson — posle retry zdes'
        job = _get_job(job_id)
        transition(job_id, "failed", fail_reason="llm_invalid_json",
                   error=str(e)[:1000], event="structure_error")
        raise


def _get_job(job_id: int) -> Job:
    with get_session() as s:
        return s.get(Job, job_id)


def _build_protocol(job_id: int) -> None:
    from ..core.llm_client import structure_transcript, llm_model
    from ..core.protocol_md import assemble_protocol
    from ..core.pipeline import transition
    p = _job_paths(job_id)
    with get_session() as s:
        segs = s.scalars(select(Segment).where(
            Segment.job_id == job_id).order_by(Segment.t_start)).all()
        job = s.get(Job, job_id)
    transcript = _transcript_lines(segs)
    data = structure_transcript(transcript, topic=job.topic)
    archive_dir = Path(os.environ.get("ARCHIVE_DIR", "/data/archive"))
    date_str = (job.ended_ts or job.start_at)
    day = date_str.strftime("%Y-%m-%d") if date_str else "unknown"
    slug = "".join(c if c.isalnum() else "-" for c in (job.topic or f"job{job_id}"))[:40] or f"job{job_id}"
    out = archive_dir / day / slug
    md, docx = assemble_protocol(job, data, segs, out)
    with get_session() as s:
        proto = s.scalars(select(Protocol).where(Protocol.job_id == job_id)).first()
        if proto is None:
            proto = Protocol(job_id=job_id)
            s.add(proto)
        proto.json_data = data
        proto.md_path = md
        proto.docx_path = docx
        proto.steno_md_path = str(p["steno"])
        proto.llm_model = llm_model()
        s.commit()
    # utochneniya (4.6) ili done
    _route_after_structure(job_id)


def _route_after_structure(job_id: int) -> None:
    from ..core.pipeline import review_requests_resolved, transition
    resolved = review_requests_resolved(job_id)
    if not resolved:
        import datetime as dt
        hours = int(os.environ.get("REVIEW_TIMEOUT_HOURS", "48") or 48)
        with get_session() as s:
            job = s.get(Job, job_id)
            job.review_deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)
            s.commit()
        transition(job_id, "awaiting_review")
    else:
        transition(job_id, "done")


def _transcript_lines(segs: list) -> str:
    out = []
    for seg in segs:
        name = seg.speaker_name or f"Golos {seg.speaker_id}"
        out.append(f"{_fmt(seg.t_start)} {name}: {seg.text}")
    return "\n".join(out)
