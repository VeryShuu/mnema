"""CRUD Job (FT-3, FT-21 biznes-logika)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import select

from .db import get_session
from .models import Job, SessionEvent
from .secrets_store import store_pwd
from .zoom_link import extract_meeting_id, normalize_join_link


def create_job(*, meeting_link: str | None = None, meeting_id: str | None = None,
               pwd: str | None = None, topic: str = "",
               start_at: dt.datetime | None = None,
               end_at: dt.datetime | None = None,
               duration_max_min: int | None = None,
               created_by: str = "api") -> Job:
    src = meeting_link or (meeting_id or "")
    if not src:
        raise ValueError("nuzhen meeting_link ili meeting_id")
    mid, extracted_pwd = extract_meeting_id(src)
    pwd = pwd or extracted_pwd
    join_link = normalize_join_link(meeting_link) if meeting_link else None
    if start_at is not None and start_at.tzinfo is None:
        from .tz import local_to_utc
        start_at = local_to_utc(start_at)  # naivnyj vzor = lokal'noe vremya operatora
    if end_at is not None and end_at.tzinfo is None:
        from .tz import local_to_utc
        end_at = local_to_utc(end_at)
    if end_at is not None and start_at is not None and end_at <= start_at:
        raise ValueError("end_at dolzhen byt' pozhe start_at")
    immediate = start_at is None
    if immediate:
        start_at = dt.datetime.now(dt.timezone.utc)
    status = "scheduled"
    with get_session() as s:
        job = Job(zoom_meeting_id=mid, join_link=join_link,
                  pwd_ref=(store_pwd(mid, pwd) if pwd else None),
                  topic=topic or f"Meeting {mid}", start_at=start_at,
                  end_at=end_at,
                  duration_max_min=duration_max_min, status=status,
                  created_by=created_by)
        s.add(job)
        s.flush()
        s.add(SessionEvent(job_id=job.id, event="created",
                           detail=f"created_by={created_by} immediate={immediate}"))
        s.commit()
        s.refresh(job)
    from .scheduler import schedule_job_for
    schedule_job_for(job.id)
    return job


def cancel_job(job_id: int) -> Job | None:
    from .pipeline import transition
    with get_session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return None
        cur = job.status
        if cur == "uploading":
            from .models import Upload
            for up in job.uploads:
                if up.status in ("pending", "uploading"):
                    up.status = "failed"
                    up.error = "otmeneno vmeste s vstrechey"
                    Path(up.path).unlink(missing_ok=True)
            s.commit()
    if cur in ("recording", "launching"):
        from . import jobs_launch
        jobs_launch.request_stop(job_id)
    if cur in ("scheduled", "launching", "recording", "processing", "uploading"):
        return transition(job_id, "cancelled", event="cancelled")
    return job
