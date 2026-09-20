"""Orkestraciya konveyera (FT-34) i validnye perekhody statusov (HT-2).

scheduled -> launching -> recording -> processing -> awaiting_llm? ->
awaiting_review? -> done | failed. Retry shagov s backoff; povtor processing
idempotenten (HT-2): segm/protokol peresozydayutsya s nulya.
"""
from __future__ import annotations

import datetime as dt
import os

from sqlalchemy import select

from .db import get_session
from .models import AudioFile, Job, Notification, Protocol, ReviewRequest, SessionEvent
from .notify import NotifyService

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "scheduled": {"launching", "cancelled", "failed", "uploading"},
    "launching": {"recording", "failed", "scheduled", "cancelled"},
    "recording": {"processing", "failed", "cancelled"},
    "uploading": {"processing", "failed", "cancelled"},
    "processing": {"awaiting_llm", "awaiting_review", "done", "failed"},
    "awaiting_llm": {"processing", "done", "failed"},
    "awaiting_review": {"done", "processing", "failed"},
    "done": set(),
    "failed": {"processing", "uploading"},
    "cancelled": set(),
}


def transition(job_id: int, new_status: str, *, error: str | None = None,
               fail_reason: str | None = None, event: str | None = None,
               notify: bool = True) -> Job:
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise KeyError(job_id)
        cur = job.status
        if new_status not in ALLOWED_TRANSITIONS.get(cur, set()):
            raise ValueError(f"nepustimaya perekhod statusa: {cur} -> {new_status}")
        job.status = new_status
        if error:
            job.error = error
        if fail_reason:
            job.fail_reason = fail_reason
        if new_status == "recording" and job.started_ts is None:
            job.started_ts = dt.datetime.now(dt.timezone.utc)
        if new_status in ("done", "failed"):
            job.ended_ts = dt.datetime.now(dt.timezone.utc)
        if event:
            s.add(SessionEvent(job_id=job_id, event=event, detail=f"status->{new_status}"))
        s.commit()
        s.refresh(job)
        snap = dict(id=job.id, topic=job.topic, mid=job.zoom_meeting_id,
                    attempts=job.join_attempts)
    if new_status == "failed" and fail_reason and notify:
        NotifyService().enqueue_job_failed(
            job_id=snap["id"], topic=snap["topic"], meeting_id=snap["mid"],
            fail_reason=fail_reason, attempts=snap["attempts"])
    if new_status == "awaiting_llm":
        _notify_awaiting_llm(job_id)
    if new_status == "done" and notify:
        _notify_protocol_ready(job_id)
    return job


def _notify_awaiting_llm(job_id: int) -> None:
    ns = NotifyService()
    with get_session() as s:
        job = s.get(Job, job_id)
        ns.enqueue("job_failed",
                   f"[Mnema] Steno gotova, LLM nedostupen: {job.topic}",
                   f"Job {job_id}: steno.md sformirovana, strukturirovanie otdozhenno "
                   f"(llm=not_configured/misconfigured). Donaistrojte LLM i zapustite "
                   f"povtor processing: {ns.base_url}/jobs/{job_id}",
                   dedup_key=f"awaiting_llm:{job_id}")


def _notify_protocol_ready(job_id: int) -> None:
    ns = NotifyService()
    with get_session() as s:
        job = s.get(Job, job_id)
        proto = s.scalars(select(Protocol).where(Protocol.job_id == job_id)).first()
    audio = f"{ns.base_url}/api/protocols/{proto.id}/audio" if proto else ""
    ns.enqueue("protocol_ready",
               f"[Mnema] Protokol gotov: {job.topic}",
               f"Protokol: {ns.base_url}/jobs/{job_id}\nAudio: {audio}",
               dedup_key=f"protocol_ready:{job_id}")


def register_recording_finished(job_id: int, *, webm_path: str | None,
                                duration_s: float | None,
                                events: list[dict] | None = None) -> None:
    """vypolnyaetsya pered transition(recording->processing): pishi audio_files + event-log."""
    with get_session() as s:
        if webm_path:
            import os as _os
            s.add(AudioFile(job_id=job_id, kind="webm", path=webm_path,
                            bytes=(_os.path.getsize(webm_path)
                                   if _os.path.exists(webm_path) else None),
                            duration_s=duration_s))
        for e in (events or []):
            s.add(SessionEvent(job_id=job_id, event=e.get("event", "media"),
                               detail=str(e.get("detail", ""))[:2000]))
        s.commit()


def review_timeout_check() -> int:
    """FT-28: neta otvetov po REVIEW_TIMEOUT_HOURS — publikuem s 'Golos N'."""
    hours = int(os.environ.get("REVIEW_TIMEOUT_HOURS", "48") or 48)
    now = dt.datetime.now(dt.timezone.utc)
    done = 0
    with get_session() as s:
        jobs = s.scalars(select(Job).where(Job.status == "awaiting_review")).all()
        for j in jobs:
            deadline = j.review_deadline
            if deadline is None:
                continue
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=dt.timezone.utc)
            if now >= deadline:
                rr = s.scalars(select(ReviewRequest).where(
                    ReviewRequest.job_id == j.id,
                    ReviewRequest.status == "pending")).all()
                for r in rr:
                    r.status = "timeout"
                j.status = "done"
                j.note = "opublikovano po taym-autu bezutochneniya (FT-28)"
                j.ended_ts = now
                done += 1
        s.commit()
    return done


def review_requests_resolved(job_id: int) -> bool:
    with get_session() as s:
        unresolved = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == job_id,
            ReviewRequest.status == "pending")).first()
        return unresolved is None
