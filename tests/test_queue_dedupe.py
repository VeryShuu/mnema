"""Testy dedupe-enqueue (mnema.core.queue): povtorny zapusk run_structure
dlya tova zhe joba ne nakanlivayet dubley v ocheredi (staraya popytka iz
proshlogo progona derzhala LLM-zamyok i novaya padala s LLMGateTimeout).

Zhivoy redis-server; skip yesli nedostupen."""
import os
import shutil
import subprocess
import time

import pytest

os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive-dedupe")
os.environ.setdefault("DATA_DIR", "/tmp/test-data-dedupe")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-dedupe.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # zavedomo niet

REDIS_BIN = shutil.which("redis-server")
DEDUPE_PORT = 6397

FUNC = "mnema.worker.tasks.run_structure"


@pytest.fixture()
def rq_env(monkeypatch):
    if REDIS_BIN is None:
        pytest.skip("redis-server nedostupen")
    proc = subprocess.Popen(
        [REDIS_BIN, "--port", str(DEDUPE_PORT), "--save", "",
         "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        from redis import Redis
        r = Redis(port=DEDUPE_PORT)
        for _ in range(100):
            try:
                r.ping()
                break
            except Exception:
                time.sleep(0.05)
        else:
            pytest.skip("redis-server ne podnyalsya")
        r.flushdb()
        monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{DEDUPE_PORT}/0")
        import mnema.core.queue as qmod
        qmod._queues.clear()
        yield r
        qmod._queues.clear()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_direct_mode_returns_none(rq_env, monkeypatch):
    from mnema.core.queue import enqueue_job
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6501/0")  # zavedomo niet
    assert enqueue_job("structure", FUNC, 26, dedupe=True) is None


def test_dedupe_returns_existing_queued_job(rq_env):
    from mnema.core.queue import enqueue_job
    first = enqueue_job("structure", FUNC, 26, dedupe=True)
    second = enqueue_job("structure", FUNC, 26, dedupe=True)
    assert first is not None and second is not None
    assert first.id == second.id, "vtoroy rerun dolzhen vorvat' sushchestvuushchuyu"
    job = first
    assert job.get_status() in ("queued", "deferred")


def test_dedupe_does_not_merge_other_jobs(rq_env):
    from mnema.core.queue import enqueue_job
    first = enqueue_job("structure", FUNC, 26, dedupe=True)
    other = enqueue_job("structure", FUNC, 23, dedupe=True)
    assert first.id != other.id


def test_without_dedupe_duplicates_stay(rq_env):
    """Klyuch dedupe=True podyomlyayet dubli; bez nego — staryy behavior."""
    from mnema.core.queue import enqueue_job
    from rq import Queue
    enqueue_job("structure", FUNC, 26)
    enqueue_job("structure", FUNC, 26)
    q = Queue("structure", connection=rq_env)
    assert q.count == 2


def test_dedupe_ignores_deferred_dependencies(rq_env):
    """Deferred (depends_on) zhivyet v otdel'nom reestre — obychnyy rerun
    ego ne naydet i sozdast svoyu queued-zadachu (raznyye smysly)."""
    from mnema.core.queue import enqueue_job
    from rq import Queue
    parent = enqueue_job("asr", "mnema.worker.tasks.run_asr_text", 26)
    joining = enqueue_job("structure", FUNC, 26, depends_on=parent)
    assert joining.get_status() == "deferred"
    rerun = enqueue_job("structure", FUNC, 26, dedupe=True)
    assert rerun is not None
    assert rerun.id != joining.id
    assert rerun.get_status() == "queued"
    assert Queue("structure", connection=rq_env).count == 1


def test_dedupe_second_rerun_after_first_picked_up(rq_env):
    """Kogda zadacha snyata s ocheredi (worker vzyal v rabotu) — dedupe ne
    meshaet postavit' novuyu popytku: started ne ubit' i ne zamenit'."""
    from mnema.core.queue import enqueue_job
    first = enqueue_job("structure", FUNC, 26, dedupe=True)
    # emulatevаем vzlyatie workerom: SNYaT' s ocheredi (kak RQ worker)
    assert rq_env.lrem("rq:queue:structure", 0, first.id) == 1
    second = enqueue_job("structure", FUNC, 26, dedupe=True)
    assert second is not None
    assert second.id != first.id
