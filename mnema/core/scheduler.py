"""Local'noye raskhanie vstrech (FT-3a) na APScheduler + import .ics.

Planirovshchik — tread' vnutri protsessa api (6.4). Postanovka Job:
za BOT_PREJOIN_SECONDS do start_at. Bez Zoom API.
"""
from __future__ import annotations

import datetime as dt
import os
import re

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .db import get_session
from .models import Job, SessionEvent
from .notify import NotifyService
from .zoom_link import extract_meeting_id


def parse_ics(text: str) -> list[dict]:
    """Minimal'nyy parser VEVENT (DTSTART/DTEND/SUMMARY/DESCRIPTION/LOCATION/UID).

    Vozvrachaet [{meeting_id, pwd, topic, start_at, duration_max_min, uid}].
    """
    from icalendar import Calendar
    events = []
    cal = Calendar.from_ical(text)
    for comp in cal.walk("VEVENT"):
        summary = str(comp.get("SUMMARY", "") or "")
        desc = str(comp.get("DESCRIPTION", "") or "")
        loc = str(comp.get("LOCATION", "") or "")
        uid = str(comp.get("UID", "") or "")
        start = comp.get("DTSTART")
        if start is None:
            continue
        start_dt = start.dt
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=dt.timezone.utc)
        end = comp.get("DTEND")
        dur_min = None
        if end is not None:
            end_dt = end.dt
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=dt.timezone.utc)
            dur_min = int((end_dt - start_dt).total_seconds() // 60)
        link_src = f"{desc} {loc} {summary}"
        mid = pwd = None
        m = re.search(r"zoom\.us/[jp]/(\d+)", link_src)
        if m:
            mid = m.group(1)
        else:
            m = re.search(r"/wc/(\d+)", link_src)
            if m:
                mid = m.group(1)
        p = re.search(r"[?&](?:pwd|passcode)=([\w-]+)", link_src)
        if p:
            pwd = p.group(1)
        events.append({"meeting_id": mid, "pwd": pwd, "topic": summary,
                       "start_at": start_dt, "duration_max_min": dur_min,
                       "uid": uid})
    return [e for e in events if e["meeting_id"]]


def schedule_job_for(job_id: int) -> None:
    """Postavit' v ochered' zapuska: za BOT_PREJOIN do start_at (ili srazu)."""
    from . import jobs_launch  # lok. import dlya uniklanija cikla
    with get_session() as s:
        job = s.get(Job, job_id)
        start_at = job.start_at
    prejoin = int(os.environ.get("BOT_PREJOIN_SECONDS", "120") or 120)
    nowts = dt.datetime.now(dt.timezone.utc)
    if start_at is None:
        run_at = nowts
    else:
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=dt.timezone.utc)
        run_at = max(start_at - dt.timedelta(seconds=prejoin), nowts)
    jobs_launch.enqueue_launch(job_id, run_at)


_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
    return _scheduler


def start_default_jobs() -> None:
    sch = get_scheduler()
    health = os.environ.get("SESSION_HEALTHCRON", "0 9 * * *").strip()
    if health:
        sch.add_job(session_health_job, CronTrigger.from_crontab(health),
                    id="session_health", replace_existing=True)
    digest = os.environ.get("ALERT_DIGEST_CRON", "0 9 * * *").strip()
    if digest:
        sch.add_job(digest_job, CronTrigger.from_crontab(digest),
                    id="alert_digest", replace_existing=True)
    # ezhechasy: review timeouts + dispatch uedomebleniy
    sch.add_job(maintenance_job, "interval", minutes=5, id="maintenance",
                replace_existing=True)
    if not sch.running:
        sch.start()


def session_health_job() -> None:
    """Health-check Zoom-sessii (FT-24): proverka cherez web-bot queue."""
    from . import jobs_launch
    jobs_launch.request_session_healthcheck()


def digest_job() -> None:
    NotifyService().build_digest()


def maintenance_job() -> None:
    from .pipeline import review_timeout_check
    from .uploads import gc_stale_uploads
    review_timeout_check()
    NotifyService().dispatch_pending()
    try:
        gc_stale_uploads()
    except Exception:  # noqa: BLE001 — GC ne dolzhen lonit' maintenance
        pass


def import_ics(text: str) -> int:
    from .jobs_api import create_job
    count = 0
    for ev in parse_ics(text):
        try:
            create_job(meeting_id=ev["meeting_id"], pwd=ev["pwd"],
                       topic=ev["topic"], start_at=ev["start_at"],
                       duration_max_min=ev["duration_max_min"],
                       created_by="ics")
            count += 1
        except Exception:  # noqa: BLE001 — propuskaem nekorrektnye sobytiya
            continue
    return count
