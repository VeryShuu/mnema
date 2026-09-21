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
    "asr": tasks.run_asr_text,
    "speaker": tasks.run_speaker_id,
    "structure": tasks.run_join,
}


def run_worker() -> None:
    names = [q.strip() for q in os.environ.get(
        "WORKER_QUEUES", "asr,speaker,structure").split(",") if q.strip()]
    r = Redis.from_url(redis_url())
    conc = max(1, int(os.environ.get("WORKER_CONCURRENCY", "2") or 2))
    print(f"rq worker start: queues={names} procs={conc}", flush=True)
    import multiprocessing as mp
    kids = [mp.Process(target=_worker_loop, args=(names,), daemon=True)
            for _ in range(conc - 1)]
    for k in kids:
        k.start()
    _start_scheduler(r, names)
    try:
        _worker_loop(names)
    finally:
        for k in kids:
            k.terminate()


def _worker_loop(names: list[str]) -> None:
    from rq import Queue, Worker
    r = Redis.from_url(redis_url())
    queues = [Queue(n, connection=r, default_timeout=4 * 3600) for n in names]
    Worker(queues, connection=r).work()


def _start_scheduler(r, names: list[str]) -> None:
    """RedisScheduler v dashneem potoke: bez nego jobs s depends_on (join po
    oboim vetkam) ostatutsya Deferred navsegda."""
    import threading
    try:
        from rq.scheduler import RQScheduler
        sched = RQScheduler(names, connection=r, interval=1)
        threading.Thread(target=sched.start, name="rq-scheduler",
                         daemon=True).start()
        print("rq scheduler started", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"rq scheduler niedostupen ({e}) — depends_on ne budet rabotat'",
              flush=True)


def run_worker_sync() -> None:
    """Single-shot: otdelyt' po odnoy zadahe iz kazhdoi ocheredi (dlya testov)."""
    from rq import Queue, SimpleWorker
    names = [q.strip() for q in os.environ.get("WORKER_QUEUES", "asr,speaker,structure").split(",") if q.strip()]
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
