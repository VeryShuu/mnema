"""Start Job: peredacha v web-bot cherez redis, kontrol' zapuskov.

web-bot vylechivaet ochered' mnema:launch; vypolnyaet zahod i pishet statusy
obratno po REST api (POST /api/jobs/{id}/events i PATCH status — vnutrennie
endpoints).
"""
from __future__ import annotations

import datetime as dt
import json

from .db import get_session
from .models import Job
from .queue import get_queue

LAUNCH_QUEUE_KEY = "mnema:launch"
STOP_QUEUE_KEY = "mnema:stop"
HEALTHCHECK_KEY = "mnema:session_healthcheck"
SALVAGE_QUEUE_KEY = "mnema:salvage"


def enqueue_launch(job_id: int, run_at: dt.datetime) -> bool:
    """Otlozhennyy start: esli vremya nastupilo — pryamo v redis-list;
    inache — APScheduler (v api) postavit' khoroshiy v remeni."""
    from redis import Redis
    from .queue import redis_url
    now = dt.datetime.now(dt.timezone.utc)
    delay = (run_at - now).total_seconds()
    if delay <= 0:
        try:
            Redis.from_url(redis_url()).lpush(
                LAUNCH_QUEUE_KEY, json.dumps({"job_id": job_id, "run_at": run_at.isoformat()}))
            return True
        except Exception:
            return False
    # otdozhennyy start: postavit' v APScheduler, on vyzvaniet' etu zhe funktsiyu v nuzhnoye vremya
    def _push() -> None:
        try:
            Redis.from_url(redis_url()).lpush(
                LAUNCH_QUEUE_KEY, json.dumps({"job_id": job_id, "run_at": run_at.isoformat()}))
        except Exception:
            pass
    from .scheduler import get_scheduler
    sch = get_scheduler()
    sch.add_job(_push, trigger="date", run_date=run_at, id=f"launch:{job_id}",
                replace_existing=True)
    return True


def request_stop(job_id: int, reason: str | None = None) -> bool:
    try:
        from redis import Redis
        from .queue import redis_url
        payload = {"job_id": job_id}
        if reason:
            payload["reason"] = reason
        Redis.from_url(redis_url()).lpush(STOP_QUEUE_KEY, json.dumps(payload))
        return True
    except Exception:
        return False


def request_session_healthcheck() -> None:
    try:
        from redis import Redis
        from .queue import redis_url
        Redis.from_url(redis_url()).lpush(HEALTHCHECK_KEY,
                                          json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat()}))
    except Exception:
        pass


def request_salvage(job_id: int, reason: str | None = None) -> bool:
    """Postavit' zadachu v Salvage ochered web-bot: web-bot probeduet syryye
    segmenty v /data/jobs/<id>/, sberezhet ih v raw.webm/meeting.wav/meeting.mp3
    i soobschit finished. Ispolzuetsya watchdog-gilya (stalled recording)."""
    try:
        from redis import Redis
        from .queue import redis_url
        payload = {"job_id": job_id}
        if reason:
            payload["reason"] = reason
        Redis.from_url(redis_url()).lpush(SALVAGE_QUEUE_KEY, json.dumps(payload))
        return True
    except Exception:
        return False


def mark_launching(job_id: int) -> None:
    from .pipeline import transition
    transition(job_id, "launching", event="launch_enqueued")


def get_launch_payloads(limit: int = 10, pop: bool = True) -> list[dict]:
    """Dlya testov i dev: pyat' iz ocheredi pryamo."""
    try:
        from redis import Redis
        from .queue import redis_url
        r = Redis.from_url(redis_url())
        out = []
        for _ in range(limit):
            item = r.rpop(LAUNCH_QUEUE_KEY) if pop else None
            if item is None:
                break
            out.append(json.loads(item))
        return out
    except Exception:
        return []
