"""Zapusk RQ (redis) ili lokal'nyy "direct" rezhim dlya testov bez redis."""
from __future__ import annotations

import os


def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://redis:6379/0").strip()


_queues: dict[str, tuple] = {}


def get_queue(name: str):
    """Vozvrachaet (queue, mode); mode='rq'|'direct'. V direct vzyvaetsya inline."""
    if name in _queues:
        return _queues[name]
    try:
        from redis import Redis
        from rq import Queue
        r = Redis.from_url(redis_url(), socket_connect_timeout=1)
        r.ping()
        q = Queue(name, connection=r, default_timeout=4 * 3600)
        _queues[name] = (q, "rq")
    except Exception:
        _queues[name] = (None, "direct")
    return _queues[name]


def enqueue_job(name: str, func, *args, depends_on=None, **kwargs):
    """Enqueue v RQ (mozhno depends_on=Job|list); v direct mode — None.

    Vozvrat: RQ Job v rq-rezhime, inache None (vyzyvatel' reshayet' sam)."""
    q, mode = get_queue(name)
    if mode == "rq":
        return q.enqueue(func, *args, depends_on=depends_on, **kwargs)
    return None
