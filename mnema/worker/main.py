"""RQ workera: roli asr/speaker/structure/archive (6.4).

WORKER_QUEUES=asr,speaker,structure,archive (po umolchaniyu vse).
"""
from __future__ import annotations

import os
import sys

from redis import Redis

from ..core.queue import redis_url
from . import tasks


QUEUES = {
    "asr": tasks.run_asr_pipeline,
    "speaker": tasks.run_asr_pipeline,
    "structure": tasks.run_structure,
}


def run_worker() -> None:
    from rq import Queue, Worker
    names = [q.strip() for q in os.environ.get(
        "WORKER_QUEUES", "asr,structure").split(",") if q.strip()]
    r = Redis.from_url(redis_url())
    queues = [Queue(n, connection=r) for n in names]
    print(f"rq worker start: queues={names}", flush=True)
    Worker(queues, connection=r,
            job_timeout="4h", max_jobs=1000).work()


def run_worker_sync() -> None:
    """Single-shot: otdelyt' po odnoy zadahe iz kazhdoi ocheredi (dlya testov)."""
    from rq import Queue, SimpleWorker
    names = [q.strip() for q in os.environ.get("WORKER_QUEUES", "asr,structure").split(",") if q.strip()]
    r = Redis.from_url(redis_url())
    queues = {n: Queue(n, connection=r) for n in names}
    done = 0
    while True:
        progressed = 0
        for q in queues.values():
            if len(q) == 0:
                continue
            worker = SimpleWorker([q], connection=r, job_timeout="4h")
            worker.dequeue_job_and_maintain_ttl(0)
            progressed += 1
        if progressed == 0:
            break
        done += 1
        if done > 500:
            break
    return done


if __name__ == "__main__":
    if "--sync" in sys.argv:
        run_worker_sync()
    else:
        run_worker()
