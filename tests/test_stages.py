"""Testy sborki etapov konveyera (mnema.core.stages) dlya web-interfeysa."""
import datetime as dt

import pytest
from sqlalchemy import create_engine, StaticPool


@pytest.fixture()
def env(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    import mnema.core.models
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    yield
    set_engine_for_test(None)


def _mk_job(status="processing", source="upload", fail_reason=None):
    from mnema.core.db import get_session
    from mnema.core.models import Job
    with get_session() as s:
        job = Job(topic="T", status=status, source=source,
                  start_at=dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc),
                  fail_reason=fail_reason)
        s.add(job)
        s.commit()
        return job.id


def _ev(job_id, event, detail=None):
    from mnema.core.db import get_session
    from mnema.core.models import SessionEvent
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event=event, detail=detail))
        s.commit()


def _states(stages):
    return {st["key"]: st["state"] for st in stages}


def test_stages_returns_all_defs_in_order(env):
    from mnema.core.stages import STAGE_DEFS, build_stages
    jid = _mk_job(status="scheduled", source="zoom")
    stages = build_stages(jid)
    assert [st["key"] for st in stages] == [k for k, _, _ in STAGE_DEFS]


def test_created_always_done(env):
    from mnema.core.stages import build_stages
    jid = _mk_job(status="scheduled", source="zoom")
    assert _states(build_stages(jid))["created"] == "done"


def test_upload_skipped_for_zoom_job(env):
    from mnema.core.stages import build_stages
    jid = _mk_job(status="processing", source="zoom")
    assert _states(build_stages(jid))["upload"] == "skipped"


def test_recording_skipped_for_upload_job(env):
    from mnema.core.stages import build_stages
    jid = _mk_job(status="processing", source="upload")
    assert _states(build_stages(jid))["recording"] == "skipped"


def test_upload_stage_running_then_done(env):
    from mnema.core.stages import build_stages
    jid = _mk_job(status="uploading", source="upload")
    assert _states(build_stages(jid))["upload"] == "running"
    _ev(jid, "upload_complete")
    assert _states(build_stages(jid))["upload"] == "done"


def test_full_processing_pipeline_states(env):
    from mnema.core.stages import build_stages
    jid = _mk_job(status="processing", source="upload")
    for e in ("vad_start", "vad_done", "asr_start", "asr_done",
              "diar_start", "diar_done", "voiceprint_start"):
        _ev(jid, e)
    st = _states(build_stages(jid))
    assert st["prep"] == "done"
    assert st["asr"] == "done"
    assert st["speaker"] == "done"
    assert st["voiceprint"] == "running"


def test_awaiting_llm_marks_llm_running(env):
    from mnema.core.stages import build_stages
    from mnema.core.db import get_session
    from mnema.core.models import Protocol
    jid = _mk_job(status="awaiting_llm", source="upload")
    for e in ("vad_done", "asr_done", "diar_done", "voiceprint_done", "llm_start"):
        _ev(jid, e)
    with get_session() as s:
        s.add(Protocol(job_id=jid, json_data={"transcript_only": True},
                       steno_md_path="/tmp/s.md"))
        s.commit()
    st = _states(build_stages(jid))
    assert st["steno"] == "done"
    assert st["llm"] == "running"


def test_llm_invalid_json_failure_marks_llm_failed(env):
    from mnema.core.stages import build_stages
    jid = _mk_job(status="failed", source="upload", fail_reason="llm_invalid_json")
    for e in ("vad_done", "asr_done", "diar_done", "steno_done", "llm_start"):
        _ev(jid, e)
    _ev(jid, "structure_error", "Server disconnected without sending a response.")
    st = build_stages(jid)
    states = _states(st)
    assert states["llm"] == "failed"
    assert states["asr"] == "done"
    assert states["done"] == "failed"
    llm = next(s for s in st if s["key"] == "llm")
    assert "disconnected" in (llm["detail"] or "")


def test_done_job_all_key_stages_done(env):
    from mnema.core.stages import build_stages
    from mnema.core.db import get_session
    from mnema.core.models import Protocol
    jid = _mk_job(status="done", source="zoom")
    for e in ("vad_done", "asr_done", "diar_done", "voiceprint_done", "steno_done",
              "llm_done"):
        _ev(jid, e)
    with get_session() as s:
        s.add(Protocol(job_id=jid, json_data={"summary": "x"}, md_path="/tmp/p.md"))
        s.commit()
    st = _states(build_stages(jid))
    assert st["llm"] == "done"
    assert st["done"] == "done"
    assert st["review"] == "skipped"


def test_awaiting_review_shows_review_running(env):
    from mnema.core.stages import build_stages
    from mnema.core.db import get_session
    from mnema.core.models import Protocol, ReviewRequest
    jid = _mk_job(status="awaiting_review", source="zoom")
    for e in ("vad_done", "asr_done", "diar_done", "llm_done"):
        _ev(jid, e)
    with get_session() as s:
        s.add(Protocol(job_id=jid, json_data={"summary": "x"}, md_path="/tmp/p.md"))
        s.add(ReviewRequest(job_id=jid, cluster_label="S0", status="pending"))
        s.commit()
    st = _states(build_stages(jid))
    assert st["review"] == "running"
    assert st["voiceprint"] == "done"


def test_asr_failure_marks_asr_failed(env):
    from mnema.core.stages import build_stages
    jid = _mk_job(status="failed", source="upload", fail_reason="asr_error")
    _ev(jid, "vad_done")
    _ev(jid, "asr_start", "engine=stub")
    _ev(jid, "asr_error", "connection refused")
    st = _states(build_stages(jid))
    assert st["asr"] == "failed"
    assert st["prep"] == "done"
    assert st["speaker"] == "pending"
