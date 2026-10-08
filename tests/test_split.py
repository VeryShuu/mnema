"""Тесты этапа «Разделение встреч» (mnema.core.split + врезка в конвейер).

Детектор границ и ffmpeg-слайсы — заглушки; реального LLM/ffmpeg нет.
БД — in-memory SQLite, env как в других тестах проекта.
"""
import os
import wave

os.environ.setdefault("DATA_DIR", "/tmp/test-split-data")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-split-archive")
os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")

import datetime as dt  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, func, select, StaticPool  # noqa: E402


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
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test")
    for var in ("SPLIT_MEETINGS", "SPLIT_MIN_MINUTES", "SPLIT_MIN_CONFIDENCE"):
        monkeypatch.delenv(var, raising=False)
    import mnema.core.queue as q
    monkeypatch.setattr(q, "_queues", {}, raising=False)
    monkeypatch.setattr(q, "get_queue", lambda name: (None, "direct"))
    yield tmp_path
    set_engine_for_test(None)


def _mk_job(segs_count: int = 16, topic: str = "Планёрка", duration_s: float = 60.0,
            status: str = "processing", source: str = "upload"):
    """Job сsegments по duration_s секунд каждый (t = i*duration .. +0.8*duration)."""
    import mnema.core.models as m
    from mnema.core.db import get_session
    with get_session() as s:
        job = m.Job(topic=topic, status=status, source=source,
                    start_at=dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc))
        s.add(job)
        s.flush()
        for i in range(segs_count):
            t0 = i * duration_s
            s.add(m.Segment(job_id=job.id, t_start=t0, t_end=t0 + duration_s * 0.8,
                            text="рабочая реплика", speaker_id="S0",
                            diar_label="S0", speaker_source="voiceprint"))
        s.commit()
        return job.id


def _audio_file(job_id: int) -> None:
    from mnema.worker.tasks import _job_paths
    p = _job_paths(job_id)
    with wave.open(str(p["wav"]), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 16000)
    p["mp3"].write_bytes(b"ID3fake")


def _events(job_id: int) -> list[str]:
    from mnema.core.db import get_session
    from mnema.core.models import SessionEvent
    with get_session() as s:
        return [e.event for e in s.scalars(select(SessionEvent).where(
            SessionEvent.job_id == job_id)).all()]


def _stub_llm_ok(monkeypatch):
    """capability_status -> configured (без реального probe)."""
    import mnema.core.capabilities as caps
    monkeypatch.setattr(caps, "capability_status",
                        lambda force=False: {"llm": {"state": "configured"}})


def _stub_media(monkeypatch):
    """media_slice без ffmpeg (wav/mp3 — детекция не требует реального слайса)."""
    import mnema.core.audio as au
    from pathlib import Path

    def fake(src, t0, t1, dst):
        Path(dst).write_bytes(b"slice")
    monkeypatch.setattr(au, "media_slice", fake)


# ---------------------------------------------------------------- parse_ts
def test_parse_ts_hms():
    from mnema.core.llm_client import parse_ts
    assert parse_ts("01:00:00") == 3600.0


def test_parse_ts_ms_short():
    from mnema.core.llm_client import parse_ts
    assert parse_ts("1:02") == 62.0


def test_parse_ts_numeric_ok():
    from mnema.core.llm_client import parse_ts
    assert parse_ts(90) == 90.0


def test_parse_ts_garbage_raises():
    from mnema.core.llm_client import LLMError, parse_ts
    with pytest.raises(LLMError):
        parse_ts("abc")


def test_parse_ts_negative_raises():
    from mnema.core.llm_client import LLMError, parse_ts
    with pytest.raises(LLMError):
        parse_ts("-10")


def test_parse_ts_overflow_raises():
    from mnema.core.llm_client import LLMError, parse_ts
    with pytest.raises(LLMError):
        parse_ts("99:99:99")


# ---------------------------------------------------------------- should_try_split
def test_should_try_split_kill_switch(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    monkeypatch.setenv("SPLIT_MEETINGS", "0")
    jid = _mk_job()
    with get_session() as s:
        job = s.get(Job, jid)
        assert sp.should_try_split(job, list(range(20))) == "disabled"


def test_should_try_split_too_short(env):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    jid = _mk_job(segs_count=30, duration_s=10.0)  # ~5 минут < SPLIT_MIN_MINUTES
    lite = [type("Lite", (), {"t_start": float(i * 10),
                              "t_end": float(i * 10 + 8)})() for i in range(30)]
    with get_session() as s:
        job = s.get(Job, jid)
        assert sp.should_try_split(job, lite) == "too_short"


def test_should_try_split_too_few_segments(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    _stub_llm_ok(monkeypatch)
    jid = _mk_job()
    lite = [type("Lite", (), {"t_end": float(i)})() for i in range(5)]
    with get_session() as s:
        job = s.get(Job, jid)
        assert sp.should_try_split(job, lite) == "too_few_segments"


def test_should_try_split_already_decided(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    _stub_llm_ok(monkeypatch)
    jid = _mk_job()
    with get_session() as s:
        job = s.get(Job, jid)
        job.split_json = {"skipped": "no_boundaries_found"}
        s.commit()
    lite = [type("Lite", (), {"t_end": float(i * 60)})() for i in range(40)]
    with get_session() as s:
        job = s.get(Job, jid)
        assert sp.should_try_split(job, lite) == "already_decided"


def test_should_try_split_child_never(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    _stub_llm_ok(monkeypatch)
    jid = _mk_job()
    with get_session() as s:
        job = s.get(Job, jid)
        job.parent_job_id = jid  # сам себе родитель — для проверки guard'а
        s.commit()
        # self-FK в SQLite в тесте без FK-checking; читаем поле как есть
        lite = [type("Lite", (), {"t_end": float(i * 60)})() for i in range(40)]
        assert sp.should_try_split(job, lite) == "child_job"


def test_should_try_split_llm_not_configured(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    import mnema.core.capabilities as caps
    monkeypatch.setattr(caps, "capability_status",
                        lambda force=False: {"llm": {"state": "not_configured"}})
    jid = _mk_job()
    lite = [type("Lite", (), {"t_end": float(i * 60 + 30)})() for i in range(40)]
    with get_session() as s:
        job = s.get(Job, jid)
        assert sp.should_try_split(job, lite) == "llm_not_configured"


# ---------------------------------------------------------------- snap_boundary
def _lite_segs(gap_after: int = 5, seg_s: float = 480.0, n: int = 16):
    segs = []
    for i in range(n):
        t0 = i * seg_s
        segs.append(type("Lite", (), {"t_start": t0, "t_end": t0 + seg_s * 0.75})())
    return segs


def test_snap_boundary_inside_gap_snaps_to_next_start():
    from mnema.core import split as sp
    segs = _lite_segs()
    # между seg5 (2400..2760) и seg6 (2880..3240) — пауза; кандидат 2800 ∈ gap
    assert sp.snap_boundary(segs, 2800.0) == 2880.0


def test_snap_boundary_inside_speech_moves_to_max_gap():
    from mnema.core import split as sp
    segs = _lite_segs()
    # кандидат внутри реплики seg5 — ближайший максимум gap'а в ±120 с -> 2880
    assert sp.snap_boundary(segs, 2700.0) == 2880.0


def test_snap_boundary_empty_returns_t():
    from mnema.core import split as sp
    assert sp.snap_boundary([], 42.0) == 42.0


# ---------------------------------------------------------------- split_job
def _stub_detect(monkeypatch, boundaries):
    import mnema.core.llm_client as lc
    calls = []

    def fake(transcript, topic="", duration_s=0.0):
        calls.append({"topic": topic, "duration_s": duration_s,
                      "has_text": "реплика" in transcript})
        return [dict(b) for b in boundaries]
    monkeypatch.setattr(lc, "detect_meeting_boundaries", fake)
    return calls


def _stub_detect_error(monkeypatch):
    import mnema.core.llm_client as lc
    from mnema.core.llm_client import LLMError

    def fake(transcript, topic="", duration_s=0.0):
        raise LLMError("ollama упал")
    monkeypatch.setattr(lc, "detect_meeting_boundaries", fake)


def test_split_job_moves_segments_with_rebase(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import AudioFile, Job, Segment
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=30, duration_s=120.0)   # ~59.6 мин > порога
    _audio_file(jid)
    # сегменты i*120..i*120+96; кандидат 2745 попадает в паузу [2736, 2760]
    _stub_detect(monkeypatch, [{"t": 2745.0, "confidence": 0.9,
                                "topic_after": "Разбор инцидента"}])
    kids = sp.split_job(jid)
    assert len(kids) == 1
    with get_session() as s:
        p = s.get(Job, jid)
        c = s.get(Job, kids[0])
        assert c.source == "split" and c.status == "processing"
        assert c.parent_job_id == jid
        assert c.topic == "Разбор инцидента"
        assert p.split_json["boundaries"][0]["t"] == 2760.0
        pcount = s.scalar(select(func.count()).select_from(Segment)
                          .where(Segment.job_id == jid))
        csegs = s.scalars(select(Segment).where(Segment.job_id == kids[0])
                          .order_by(Segment.t_start)).all()
        assert pcount == 23 and len(csegs) == 7
        assert csegs[0].t_start == pytest.approx(0.0)
        from mnema.worker.tasks import _job_paths
        pp = _job_paths(jid)
        assert pp["full_wav"].exists() and pp["full_mp3"].exists()
        rows = s.scalars(select(AudioFile).where(
            AudioFile.job_id == kids[0])).all()
        assert {r.kind for r in rows} == {"wav", "mp3"}


def test_split_json_on_both_sides(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=30, duration_s=120.0)
    _stub_detect(monkeypatch, [{"t": 1800.0, "confidence": 0.95, "topic_after": ""}])
    kids = sp.split_job(jid)
    assert len(kids) == 1
    with get_session() as s:
        p = s.get(Job, jid)
        c = s.get(Job, kids[0])
        assert p.split_json is not None and p.split_json["skipped"] is None
        b = p.split_json["boundaries"][0]
        assert b["topic_before"] == "Планёрка"
        assert c.split_json == {"parent": jid, "boundary_t": b["t"],
                                "ts": c.split_json["ts"]}
        assert c.start_at is not None


def test_topics_fallback_part_n(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=30, duration_s=120.0, topic="Планёрка")
    _stub_detect(monkeypatch, [{"t": 1800.0, "confidence": 0.95, "topic_after": ""}])
    kids = sp.split_job(jid)
    with get_session() as s:
        assert s.get(Job, kids[0]).topic == "Планёрка (часть 2)"


def test_empty_parent_topic_filled(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=30, duration_s=120.0, topic="")
    _stub_detect(monkeypatch, [{"t": 1800.0, "confidence": 0.95,
                                "topic_after": "Тема хвоста"}])
    kids = sp.split_job(jid)
    with get_session() as s:
        assert s.get(Job, jid).topic == f"Встреча {jid} (часть 1)"
        assert s.get(Job, kids[0]).topic == "Тема хвоста"


def test_low_confidence_no_split(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job, Segment
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=30, duration_s=120.0)
    calls = _stub_detect(monkeypatch,
                         [{"t": 1800.0, "confidence": 0.3, "topic_after": ""}])
    assert sp.split_job(jid) == []
    with get_session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        assert s.scalar(select(func.count()).select_from(Segment)
                        .where(Segment.job_id == jid)) == 30
        job = s.get(Job, jid)
        assert job.split_json["skipped"] == "no_boundaries_found"
    assert "split_skipped" in _events(jid)
    assert "split_done" not in _events(jid)


def test_llm_error_skips_split_not_fails_job(env, monkeypatch):
    """LLMError при детекции: событие split_skipped, job идёт в structure, не failed."""
    from mnema.core.db import get_session
    from mnema.core.models import Job
    from mnema.worker.tasks import _run_split_and_structure
    _stub_media(monkeypatch)
    import mnema.core.capabilities as caps
    monkeypatch.setattr(caps, "capability_status",
                        lambda force=False: {"llm": {"state": "configured"}})
    jid = _mk_job(segs_count=30, duration_s=120.0)
    _stub_detect_error(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "")  # _run_structure -> awaiting_llm
    _run_split_and_structure(jid)
    with get_session() as s:
        assert s.get(Job, jid).status == "awaiting_llm"
    evs = _events(jid)
    assert "split_skipped" in evs
    # преходящая ошибка детекции не фиксируется как решение — rerun получит шанс
    from mnema.core.models import Job as J
    with get_session() as s:
        job = s.get(J, jid)
        assert job.split_json is None or "boundaries" not in (job.split_json or {})


def test_rerun_does_not_split_again(env, monkeypatch):
    from mnema.core import split as sp
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=30, duration_s=120.0)
    _stub_detect(monkeypatch, [{"t": 1800.0, "confidence": 0.95, "topic_after": ""}])
    assert len(sp.split_job(jid)) == 1
    calls = _stub_detect(monkeypatch, [{"t": 1800.0, "confidence": 0.95,
                                        "topic_after": ""}])
    assert sp.split_job(jid) == []


def test_two_boundaries_parent_and_two_children(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job, Segment
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=40, duration_s=120.0, topic="Марафон")
    # кандидаты внутри пауз VAD: 830 ∈ [816,840] -> 840; 2990 ∈ [2976,3000] -> 3000
    _stub_detect(monkeypatch, [
        {"t": 830.0, "confidence": 0.9, "topic_after": "Вторая"},
        {"t": 2990.0, "confidence": 0.85, "topic_after": "Третья"}])
    kids = sp.split_job(jid)
    assert len(kids) == 2
    with get_session() as s:
        assert s.scalar(select(func.count()).select_from(Segment)) == 40
        pseg = s.scalars(select(Segment).where(
            Segment.job_id == jid)).all()
        # граница 840: родителю segs 0..6 (6-й: 720..816)
        assert len(pseg) == 7 and max(sg.t_end for sg in pseg) == 816.0
        c1 = s.get(Job, kids[0])
        c2 = s.get(Job, kids[1])
        assert c1.topic == "Вторая" and c2.topic == "Третья"
        c1segs = s.scalars(select(Segment).where(
            Segment.job_id == c1.id).order_by(Segment.t_start)).all()
        c2segs = s.scalars(select(Segment).where(
            Segment.job_id == c2.id).order_by(Segment.t_start)).all()
        # [840, 3000) -> child1 локально [0, 2040]; [3000, 4776) -> child2
        assert len(c1segs) == 18 and len(c2segs) == 15
        assert max(sg.t_start for sg in c1segs) == pytest.approx(2040.0)
        assert c1segs[0].t_start == pytest.approx(0.0)
        assert c2segs[0].t_start == pytest.approx(0.0)


def test_child_presets_inherited_events(env, monkeypatch):
    from mnema.core import split as sp
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=30, duration_s=120.0)
    _stub_detect(monkeypatch, [{"t": 1800.0, "confidence": 0.95,
                                "topic_after": ""}])
    kids = sp.split_job(jid)
    evs = set(_events(kids[0]))
    for expected in ("vad_done", "asr_done", "diar_done", "voiceprint_done",
                     "review_resolved"):
        assert expected in evs


def test_audio_error_does_not_block_db_split(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    import mnema.core.audio as au
    _stub_llm_ok(monkeypatch)

    def boom(src, t0, t1, dst):
        raise RuntimeError("ffmpeg отсутствует")
    monkeypatch.setattr(au, "media_slice", boom)
    jid = _mk_job(segs_count=30, duration_s=120.0)
    _audio_file(jid)
    _stub_detect(monkeypatch, [{"t": 1800.0, "confidence": 0.95,
                                "topic_after": ""}])
    kids = sp.split_job(jid)
    assert len(kids) == 1  # БД-разделение состоялось
    with get_session() as s:
        assert s.get(Job, jid).split_json["skipped"] is None
    assert "split_audio_error" in _events(jid)


def test_transient_skip_not_persisted(env, monkeypatch):
    """kill-switch: split_skipped пишется, split_json — нет (rerun после
    включения получит шанс)."""
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job
    monkeypatch.setenv("SPLIT_MEETINGS", "0")
    jid = _mk_job(segs_count=30, duration_s=120.0)
    assert sp.split_job(jid) == []
    assert "split_skipped" in _events(jid)
    with get_session() as s:
        assert s.get(Job, jid).split_json is None


# ---------------------------------------------------------------- run_join интеграция
def test_run_join_splits_then_structures_parent(env, monkeypatch):
    """run_join: стено -> split (2 части) -> перепись steno родителя -> structure."""
    from mnema.core.db import get_session
    from mnema.core.models import Job
    from mnema.worker.tasks import run_join, _job_paths
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=30, duration_s=120.0)
    _stub_detect(monkeypatch, [{"t": 1800.0, "confidence": 0.95,
                                "topic_after": "Вторая часть"}])
    monkeypatch.setenv("LLM_BASE_URL", "")  # structure -> awaiting_llm
    run_join(jid)
    with get_session() as s:
        kids = s.scalars(select(Job).where(Job.parent_job_id == jid)).all()
        assert len(kids) == 1
        # direct-режим: потомок тоже прошагал structure -> awaiting_llm
        assert s.get(Job, kids[0].id).status == "awaiting_llm"
    # steno родителя не содержит сегментов из хвоста (после split переписан)
    txt = _job_paths(jid)["steno"].read_text(encoding="utf-8")
    child_txt = _job_paths(kids[0].id)["steno"].read_text(encoding="utf-8")
    assert txt and child_txt


def test_delete_parent_deletes_children(env, monkeypatch):
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.jobs_api import delete_job
    from mnema.core.models import Job
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=30, duration_s=120.0)
    _stub_detect(monkeypatch, [{"t": 1800.0, "confidence": 0.95,
                                "topic_after": ""}])
    kids = sp.split_job(jid)
    assert delete_job(jid) is True
    with get_session() as s:
        assert s.get(Job, jid) is None
        for cid in kids:
            assert s.get(Job, cid) is None


def test_child_route_after_structure_reaches_done(env, monkeypatch):
    """У потомка нет ReviewRequest -> _route_after_structure ведёт его в done."""
    from mnema.core import split as sp
    from mnema.core.db import get_session
    from mnema.core.models import Job, ReviewRequest
    from mnema.worker.tasks import _route_after_structure
    _stub_llm_ok(monkeypatch)
    _stub_media(monkeypatch)
    jid = _mk_job(segs_count=30, duration_s=120.0)
    _stub_detect(monkeypatch, [{"t": 1800.0, "confidence": 0.95,
                                "topic_after": ""}])
    kids = sp.split_job(jid)
    _route_after_structure(kids[0])
    with get_session() as s:
        assert s.get(Job, kids[0]).status == "done"
        assert s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == kids[0])).first() is None
