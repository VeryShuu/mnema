"""Zadachi konveyera (FT-34) dlya RQ-workera (roli: asr/speaker/structure/archive).

Kazhdaya zadacha idempotentna (HT-2): povtornyy zabeg peresozdayet rezultat s
nulya. Vne-zavisimosti (gigastt/ollama) — capability, FT-39.
"""
from __future__ import annotations

import os
import shutil
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
def run_orchestrate(job_id: int) -> None:
    """Tochka vhoda konveyera: prep+VAD, zatem parallel'nyye vetki asr/speaker,
    zatem join (steno+structure). V direct-rezhime (bez redis) — posledovatel'no.

    Vetki pisayut v raznye kolonki odnih i tekh zhe segmтов: asr — text/lang/
    engine/confidence, speaker — speaker_*/diar_label/needs_review. Join
    zapuskayetsya cherez RQ depends_on posle oboikh vetok (wait-all)."""
    try:
        run_prep(job_id)
        from ..core.queue import get_queue
        _, mode = get_queue("speaker")
        if mode == "rq":
            from ..core.queue import enqueue_job
            ja = enqueue_job("asr", "mnema.worker.tasks.run_asr_text", job_id)
            js = enqueue_job("speaker", "mnema.worker.tasks.run_speaker_id", job_id)
            deps = [d for d in (ja, js) if d is not None]
            enqueue_job("structure", "mnema.worker.tasks.run_join", job_id,
                        depends_on=deps)
        else:
            run_asr_text(job_id)
            run_speaker_id(job_id)
            run_join(job_id)
    except Exception as e:  # noqa: BLE001
        from ..core.pipeline import transition
        transition(job_id, "failed", fail_reason=_classify(e),
                   error=str(e)[:1000], event="asr_error")
        raise


def run_prep(job_id: int) -> list[dict]:
    """Raspadovka (wav/mp3) + VAD + sozdanie rows segmentov (idempotentno)."""
    p = _job_paths(job_id)
    _prepare_wav(job_id, p)
    _maybe_drop_raw_source(job_id, p)
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="vad_start"))
        s.commit()
    from ..core.vad import segment_speech
    wav = p["wav"]
    raw_segs = segment_speech(wav) if wav.exists() else []
    with get_session() as s:
        s.query(Segment).filter(Segment.job_id == job_id).delete()
        for seg in raw_segs:
            s.add(Segment(job_id=job_id, raw_track="web-mix",
                          t_start=seg["t_start"], t_end=seg["t_end"], text="",
                          speaker_source="unresolved"))
        s.add(SessionEvent(job_id=job_id, event="vad_done",
                           detail=f"{len(raw_segs)} segm"))
        s.commit()
    return raw_segs


def _job_segments(job_id: int) -> list:
    with get_session() as s:
        return s.scalars(select(Segment).where(Segment.job_id == job_id)
                         .order_by(Segment.t_start, Segment.id)).all()


def _fail_branch(job_id: int, e: Exception, event: str) -> None:
    from ..core.pipeline import ALLOWED_TRANSITIONS, transition
    from ..core.db import get_session as _gs
    with _gs() as s:
        job = s.get(Job, job_id)
        if job is None or job.status in ("failed", "done", "cancelled") \
                or "failed" not in ALLOWED_TRANSITIONS.get(job.status, set()):
            return  # uzhe zahvachennoy drugoy vetkoy — ne prevrashchat' v failed->failed
    transition(job_id, "failed", fail_reason=_classify(e),
               error=str(e)[:1000], event=event)


def run_asr_text(job_id: int) -> None:
    """Vetka transkribirovaniya: VAD-segmenty -> text (gigastt/faster-whisper)."""
    try:
        p = _job_paths(job_id)
        wav = p["wav"]
        segs = _job_segments(job_id)
        if not segs:
            return
        from ..core.asr_client import transcribe_segments, engine_name
        engine = engine_name()
        with get_session() as s:
            s.add(SessionEvent(job_id=job_id, event="asr_start",
                               detail=f"engine={engine}"))
            s.commit()
        vad = [{"t_start": s.t_start, "t_end": s.t_end} for s in segs]
        results = transcribe_segments(wav, vad) if wav.exists() else []
        with get_session() as s:
            rows = s.scalars(select(Segment).where(Segment.job_id == job_id)
                             .order_by(Segment.t_start, Segment.id)).all()
            for row, txt in zip(rows, results):
                row.text = txt.get("text", "")
                row.lang = txt.get("lang")
                row.engine = engine
                row.confidence = txt.get("confidence")
            s.add(SessionEvent(job_id=job_id, event="asr_done",
                               detail=f"engine={engine}"))
            s.commit()
    except Exception as e:  # noqa: BLE001
        _fail_branch(job_id, e, "asr_error")
        raise


def run_speaker_id(job_id: int) -> None:
    """Vetka govoraschih: diarizaciya (kak govoril) + voiceprint-matsching (kto)."""
    try:
        p = _job_paths(job_id)
        wav = p["wav"]
        segs = _job_segments(job_id)
        if not segs:
            return
        vad = [{"t_start": s.t_start, "t_end": s.t_end} for s in segs]
        from ..core.diarize import diarize
        with get_session() as s:
            s.add(SessionEvent(job_id=job_id, event="diar_start",
                               detail=f"{len(segs)} segm"))
            s.commit()
        try:
            labels = diarize(wav, vad)
        except Exception as e:  # noqa: BLE001
            print(f"diarize fail ({e}) — vse SPEAKER_UNKNOWN", flush=True)
            labels = ["SPEAKER_UNKNOWN"] * len(segs)
        with get_session() as s:
            rows = s.scalars(select(Segment).where(Segment.job_id == job_id)
                             .order_by(Segment.t_start, Segment.id)).all()
            for row, label in zip(rows, labels):
                row.speaker_id = label
                row.diar_label = label
            s.add(SessionEvent(job_id=job_id, event="diar_done",
                               detail=f"{len(set(labels))} klaster"))
            s.commit()
        _run_speaker(job_id)
    except Exception as e:  # noqa: BLE001
        _fail_branch(job_id, e, "speaker_error")
        raise


def run_join(job_id: int) -> None:
    """Sliyaniye vetok posle oboikh: steno.md, zatem LLM — tol'ko kogda vse
    govoryashchiye opredeleny. Pri neraspznannyh golosakh stop v awaiting_review
    (analiz LLM idet POSLE utochneniya lyudey, a ne do nego)."""
    try:
        p = _job_paths(job_id)
        _write_steno(job_id, p)
        if _route_awaiting_review_if_unresolved(job_id):
            return
        _run_structure(job_id)
    except Exception as e:  # noqa: BLE001
        _fail_branch(job_id, e, "processing_error")
        raise


def _route_awaiting_review_if_unresolved(job_id: int) -> bool:
    """True esli est' pending review-request: status -> awaiting_review s deadlaynom."""
    from ..core.pipeline import review_requests_resolved, transition
    if review_requests_resolved(job_id):
        return False
    import datetime as dt
    hours = int(os.environ.get("REVIEW_TIMEOUT_HOURS", "48") or 48)
    with get_session() as s:
        job = s.get(Job, job_id)
        job.review_deadline = (dt.datetime.now(dt.timezone.utc)
                               + dt.timedelta(hours=hours))
        s.commit()
    transition(job_id, "awaiting_review")
    return True


def run_asr_pipeline(job_id: int) -> None:
    """Posledovatel'nyy rezhim (dev/tests/bez redis): vetsi vot poryadke."""
    try:
        run_prep(job_id)
        run_asr_text(job_id)
        run_speaker_id(job_id)
        run_join(job_id)
    except Exception as e:  # noqa: BLE001
        _fail_branch(job_id, e, "asr_error")
        raise


def _classify(e: Exception) -> str:
    txt = str(e).lower()
    if "asr" in txt or "transcription" in txt:
        return "asr_error"
    return "processing_error"


def _maybe_drop_raw_source(job_id: int, p: dict) -> None:
    """KEEP_UPLOADED_SOURCE=0: udalit' ishodnyy arhiv (i ego AudioFile-row)
    posle uspechnogo izvlecheniya meeting.wav/mp3. Tol'ko dlya source=upload.

    Udalenie po puti (ne po kind), chtoby ne zadet' distributivnye rowy
    meeting.wav/meeting.mp3."""
    from sqlalchemy import select as _select
    from ..core.models import Upload
    from ..core.uploads import keep_source
    if keep_source():
        return
    if not p["wav"].exists() or not p["mp3"].exists():
        return
    raw = _find_raw(p["dir"])
    if raw is None:
        return
    if str(raw) == str(p["wav"]) or str(raw) == str(p["mp3"]):
        return  # raw i est' distributiv — ne udalyaem
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None or job.source != "upload":
            return
        s.query(AudioFile).filter(AudioFile.job_id == job_id,
                                  AudioFile.path == str(raw)).delete()
        for up in s.scalars(_select(Upload).where(Upload.job_id == job_id)).all():
            if up.status == "complete" and up.path == str(raw):
                up.path = ""
        s.commit()
    raw.unlink(missing_ok=True)


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink vmesto kopii (obychno tot zhe volume /data): ekonomiya v GB.
    Pri nevozmozhnosti hardlinka (raznye FS) — copyfile."""
    if dst.exists():
        dst.unlink()
    try:
        os.link(str(src), str(dst))
    except OSError:
        shutil.copyfile(str(src), str(dst))


def _find_raw(job_dir: Path) -> Path | None:
    """Nayti ishodnyy fayl konveyera: raw.webm (zapis') ili raw.<ext> (arhiv-zagruzka).

    Proizvodnye fayly konveyera nazyvayutsya meeting.*, togda kak raw.* sozdayet
    tol'ko finalize zagruzki (ili web-bot) — lyuboy raw.<ext> eto istoch."""
    from ..core.audio import MEDIA_EXTENSIONS
    cand = job_dir / "raw.webm"
    if cand.exists():
        return cand
    for f in sorted(job_dir.glob("raw.*")):
        if f.suffix.lower() in MEDIA_EXTENSIONS:
            return f
    return None


def _prepare_wav(job_id: int, p: dict) -> None:
    """Ishodnik (webm-zapis' ili arhivnyy video/audio) -> wav/mp3 (FT-6 + FT-41)."""
    from ..core.audio import webm_to_wav16k, webm_to_mp3, repair_webm, wav_duration_s
    from ..core.audio import media_to_wav16k, media_to_mp3
    raw = _find_raw(p["dir"])
    if raw is None:
        return
    if p["wav"].exists() and p["wav"].stat().st_size > 44:
        return
    is_webm = raw.suffix.lower() == ".webm"
    if is_webm:
        if not _webm_ok(raw):
            repair_webm(raw)
        webm_to_wav16k(raw, p["wav"])
        dur = wav_duration_s(p["wav"])
        if not p["mp3"].exists():
            ok = webm_to_mp3(raw, p["mp3"]) or _wav_to_mp3(p)
            if not ok:
                pass  # mp3 enc niedostupen — vydayem wav kak distributiv (FT-41a)
    elif raw.suffix.lower() == ".wav":
        # arhivnyy wav lyubogo formata: peresampleiruem v canonical'nyy 16k mono
        media_to_wav16k(raw, p["wav"])
        dur = wav_duration_s(p["wav"])
        if not p["mp3"].exists():
            media_to_mp3(raw, p["mp3"]) or _wav_to_mp3(p)
    elif raw.suffix.lower() == ".mp3":
        from ..core.audio import ffprobe_duration
        if raw != p["mp3"]:
            _link_or_copy(raw, p["mp3"])  # gotovaya dorozhka dlya vydachi
        dur = ffprobe_duration(raw)
        media_to_wav16k(raw, p["wav"])
    else:
        # video/konteyner: izvlekayem pervuyu audio-dorozhku
        media_to_wav16k(raw, p["wav"])
        dur = wav_duration_s(p["wav"])
        if not p["mp3"].exists():
            media_to_mp3(raw, p["mp3"])
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
    """Vozvrat posle utochneniya spikero: steno.md s itogovymi imenami + LLM."""
    try:
        _write_steno(job_id, _job_paths(job_id))
        _run_structure(job_id)
    except Exception as e:  # noqa: BLE001
        _fail_branch(job_id, e, "processing_error")
        raise


def _run_speaker(job_id: int) -> None:
    """FT-25..28: emgeddingi klasteov + matsching + review zapolnenie."""
    from ..core.speaker import (compute_embedding_for_file, match_cluster,
                                thresholds)
    p = _job_paths(job_id)
    wav = p["wav"]
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="voiceprint_start"))
        s.commit()
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
    from ..core.db import get_session as _gs
    with _gs() as s:
        s.add(SessionEvent(job_id=job_id, event="voiceprint_done"))
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
            Segment.job_id == job_id, Segment.excluded.is_(False)
        ).order_by(Segment.t_start)).all()
        job = s.get(Job, job_id)
    lines = [f"# Protocol (transkript): {job.topic}", ""]
    for seg in segs:
        name = seg.speaker_name or f"Golos {seg.speaker_id}"
        tag = " (trebuetsya utochnenie)" if seg.needs_review else ""
        lines.append(f"{_fmt(seg.t_start)} {name}{tag}: {seg.text}")
    p["steno"].write_text("\n".join(lines), encoding="utf-8")
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="steno_done",
                           detail=str(p["steno"])))
        s.commit()
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
        # LLMvernul neuralid Nson / otsvet ne doshol: stenogramma uzhe na
        # diske — ne tonim job, a dosylаем v awaiting_llm (donastraivaemyj shut).
        from ..core.llm_client import LLMError
        if not isinstance(e, LLMError):
            transition(job_id, "failed", fail_reason="llm_invalid_json",
                       error=str(e)[:1000], event="structure_error")
            raise
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
            Segment.job_id == job_id, Segment.excluded.is_(False)
        ).order_by(Segment.t_start)).all()
        job = s.get(Job, job_id)
    transcript = _transcript_lines(segs)
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="llm_start",
                           detail=f"{len(segs)} segm"))
        s.commit()
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
        s.add(SessionEvent(job_id=job_id, event="llm_done",
                           detail=f"model={llm_model()}"))
        s.commit()
    # utochneniya (4.6) ili done
    _route_after_structure(job_id)


def _route_after_structure(job_id: int) -> None:
    from ..core.pipeline import transition
    if not _route_awaiting_review_if_unresolved(job_id):
        transition(job_id, "done")


def _transcript_lines(segs: list) -> str:
    out = []
    for seg in segs:
        name = seg.speaker_name or f"Golos {seg.speaker_id}"
        out.append(f"{_fmt(seg.t_start)} {name}: {seg.text}")
    return "\n".join(out)
