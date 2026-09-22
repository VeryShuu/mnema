"""LLM-analiz idet POSLE opredeleniya govoryashchihih (portirovka konveyera).

run_join ostanavlivaet job v awaiting_review pri neraspoznannyh golosah;
posle zapresheniya vseh review (otvet ili taym-aut) konveyer vozvraschaetsya
v processing i vypolnyaet step strukturirovaniya (LLM).
"""
import os

import pytest
from sqlalchemy import create_engine, select, StaticPool

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    import mnema.core.models  # noqa: F401
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("LLM_BASE_URL", "")
    # direct mode + zaglushka enqueue, chtoby videt' fakt postanovki v ochered'
    import mnema.core.queue as q
    monkeypatch.setattr(q, "_queues", {}, raising=False)
    monkeypatch.setattr(q, "get_queue", lambda name: (None, "direct"))
    yield tmp_path


def _mkjob(segs_count: int = 2, with_review: bool = True):
    import mnema.core.models as m
    from mnema.core.db import get_session
    with get_session() as s:
        job = m.Job(topic="T", status="processing", source="upload")
        s.add(job)
        s.flush()
        for i in range(segs_count):
            s.add(m.Segment(job_id=job.id, t_start=i * 5.0, t_end=i * 5.0 + 3.0,
                            text="slova", speaker_id="S0", diar_label="S0",
                            speaker_source="unresolved", needs_review=True))
        if with_review:
            s.add(m.ReviewRequest(job_id=job.id, cluster_label="S0",
                                  similarity=0.2, status="pending"))
        s.commit()
        return job.id


def test_join_stops_before_llm_when_speakers_unresolved(env):
    """Neraspznannye golosa -> awaiting_review, step strukturirovaniya NE zapeschen."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, Protocol, SessionEvent
    from mnema.worker.tasks import run_join

    jid = _mkjob()
    run_join(jid)
    with get_session() as s:
        assert s.get(Job, jid).status == "awaiting_review"
        assert s.get(Job, jid).review_deadline is not None
        assert s.scalars(select(Protocol).where(Protocol.job_id == jid)).first() is None
        events = {e.event for e in s.scalars(
            select(SessionEvent).where(SessionEvent.job_id == jid)).all()}
        assert "steno_done" in events
        assert "llm_start" not in events


def test_join_runs_llm_when_all_speakers_resolved(env):
    """Net pending review -> strukturirovanie vypolnyaetsya srazu (done/awaiting_llm)."""
    from mnema.core.db import get_session
    from mnema.core.models import Job
    from mnema.worker.tasks import run_join

    jid = _mkjob(with_review=False)
    run_join(jid)
    with get_session() as s:
        # LLM not_configured -> awaiting_llm (shag protyden, golosa opredeleny)
        assert s.get(Job, jid).status == "awaiting_llm"


def test_resume_after_last_review_enqueues_structure(env, monkeypatch):
    """Posledniy otvet -> processing + enqueue run_structure (analiz LLM posle utochneniya)."""
    from mnema.core.db import get_session
    from mnema.core.models import Job
    from mnema.core.pipeline import resume_after_reviews
    from mnema.worker.tasks import run_join

    called = []
    import mnema.core.queue as q
    monkeypatch.setattr(q, "enqueue_job",
                        lambda name, func, *a, **k: called.append((name, func, a)))

    jid = _mkjob()
    run_join(jid)
    # otvechen ne posledniy — eshche est' pending? odin klaster: zakryvaem ego
    from mnema.core.models import ReviewRequest
    with get_session() as s:
        rr = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == jid)).first()
        rr1 = ReviewRequest(job_id=jid, cluster_label="S1", similarity=0.1)
        s.add(rr1)
        rr.status = "confirmed"
        s.commit()
    assert resume_after_reviews(jid) is False
    assert called == []
    with get_session() as s:
        s.get(Job, jid).status = "awaiting_review"  # posle False nikto ne dvigal
        s.commit()
    with get_session() as s:
        rr1 = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == jid,
            ReviewRequest.cluster_label == "S1")).first()
        rr1.status = "confirmed"
        s.commit()
    assert resume_after_reviews(jid) is True
    assert called and called[0][0] == "structure"
    assert called[0][1] == "mnema.worker.tasks.run_structure"
    with get_session() as s:
        assert s.get(Job, jid).status == "processing"


def test_review_timeout_releases_job_into_llm(env, monkeypatch):
    """Taym-aut review ne publikuet kak done: job uhoit v processing s enqueue."""
    import datetime as dt

    from mnema.core.db import get_session
    from mnema.core.models import Job, ReviewRequest
    from mnema.core.pipeline import review_timeout_check

    called = []
    import mnema.core.queue as q
    monkeypatch.setattr(q, "enqueue_job",
                        lambda name, func, *a, **k: called.append((name, func, a)))

    jid = _mkjob()
    with get_session() as s:
        j = s.get(Job, jid)
        j.status = "awaiting_review"
        j.review_deadline = dt.datetime.now(
            dt.timezone.utc) - dt.timedelta(hours=1)
        s.commit()
    n = review_timeout_check()
    assert n == 1
    with get_session() as s:
        j = s.get(Job, jid)
        assert j.status == "processing"
        for r in s.scalars(select(ReviewRequest).where(
                ReviewRequest.job_id == jid)).all():
            assert r.status == "timeout"
    assert called and called[0][1] == "mnema.worker.tasks.run_structure"


def test_timeout_skips_job_before_deadline(env, monkeypatch):
    import datetime as dt

    from mnema.core.db import get_session
    from mnema.core.models import Job
    from mnema.core.pipeline import review_timeout_check

    import mnema.core.queue as q
    monkeypatch.setattr(q, "enqueue_job", lambda *a, **k: None)

    jid = _mkjob()
    with get_session() as s:
        j = s.get(Job, jid)
        j.status = "awaiting_review"
        j.review_deadline = dt.datetime.now(
            dt.timezone.utc) + dt.timedelta(hours=1)
        s.commit()
    assert review_timeout_check() == 0
    with get_session() as s:
        assert s.get(Job, jid).status == "awaiting_review"
