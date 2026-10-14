"""Watchdog zalishchayushchikh `recording` (stalled_recording_watch).

Zadacha v recording bez novykh sobytiy dolshe STALLED_RECORDING_MIN ->
salvage-ochered'; svezhee sobytie (lyuboye) reset taymera; povtorny watchdog
srazu ne spamit (salvage_requested — sobytie, kassy reset). Redis niet —
request_salvage monkeypatches.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import StaticPool, create_engine, select

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive")
os.environ.setdefault("DATA_DIR", "/tmp/test-data")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-stalled.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # zavedomo niet
os.environ["LLM_BASE_URL"] = ""
os.environ["ASR_BASE_URL"] = ""


@pytest.fixture()
def db(tmp_path, monkeypatch):
    # modeli DO create_all: inache tabshe ne budet vmete (metadata pusta)
    import mnema.core.models  # noqa: F401
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEBBOT_HEARTBEAT_FILE", str(tmp_path / "hb"))
    return engine


def _recording_job(started_min_ago: int) -> int:
    from mnema.core.db import get_session
    from mnema.core.models import Job, SessionEvent
    from mnema.core.pipeline import transition
    with get_session() as s:
        j = Job(zoom_meeting_id="123456789", topic="Тест watchdog")
        s.add(j)
        s.commit()
        jid = j.id
    transition(jid, "launching")
    transition(jid, "recording")
    with get_session() as s:
        j = s.get(Job, jid)
        j.started_ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=started_min_ago)
        # dvizhem i edinstvennoye sobytie, chtoby «posledneye sobytie» bylo tishim
        ev = s.scalars(select(SessionEvent).where(
            SessionEvent.job_id == jid)).first()
        if ev:
            ev.ts = j.started_ts
        s.commit()
    return jid


def _salvage_calls(monkeypatch) -> list:
    calls = []

    def fake(job_id, reason=None):
        calls.append((job_id, reason))
        return True
    monkeypatch.setattr("mnema.core.jobs_launch.request_salvage", fake)
    return calls


def test_stalled_recording_postavlyaet_v_salvage_ochered(db, monkeypatch):
    monkeypatch.setenv("STALLED_RECORDING_MIN", "20")
    calls = _salvage_calls(monkeypatch)
    jid = _recording_job(started_min_ago=45)
    from mnema.core.pipeline import stalled_recording_watch
    assert stalled_recording_watch() == 1
    assert calls == [(jid, "stalled_recording")]


def test_svezhaya_zapis_ne_tremaetsya(db, monkeypatch):
    monkeypatch.setenv("STALLED_RECORDING_MIN", "20")
    calls = _salvage_calls(monkeypatch)
    _recording_job(started_min_ago=5)
    from mnema.core.pipeline import stalled_recording_watch
    assert stalled_recording_watch() == 0
    assert calls == []


def test_povtorny_watchdog_ne_spamit_posle_salvage_requested(db, monkeypatch):
    monkeypatch.setenv("STALLED_RECORDING_MIN", "20")
    calls = _salvage_calls(monkeypatch)
    _recording_job(started_min_ago=45)
    from mnema.core.pipeline import stalled_recording_watch
    assert stalled_recording_watch() == 1
    # vtoroy prohod srazu: posledneye sobytie (salvage_requested) svezhee —
    # taymer sbrosen, povtoroy otkazki net
    assert stalled_recording_watch() == 0
    assert len(calls) == 1


def test_redis_otkaz_ne_piset_marku_i_povtoryaetsya(db, monkeypatch):
    # request_salvage vernul False (redis nedostupen) — marku 'salvage_requested'
    # NE pishem: taymer tishiny ne sbrosyetsya, sleduyushchiy protih poprovit snova
    monkeypatch.setenv("STALLED_RECORDING_MIN", "20")
    monkeypatch.setattr("mnema.core.jobs_launch.request_salvage",
                        lambda job_id, reason=None: False)
    _recording_job(started_min_ago=45)
    from mnema.core.db import get_session
    from mnema.core.models import SessionEvent
    from mnema.core.pipeline import stalled_recording_watch
    assert stalled_recording_watch() == 0
    with get_session() as s:
        marks = s.scalars(select(SessionEvent).where(
            SessionEvent.event == "salvage_requested")).all()
        assert marks == []


def test_ne_chuzye_statusy_ne_tromaemsya(db, monkeypatch):
    monkeypatch.setenv("STALLED_RECORDING_MIN", "20")
    calls = _salvage_calls(monkeypatch)
    jid = _recording_job(started_min_ago=45)
    from mnema.core.db import get_session
    from mnema.core.models import Job
    from mnema.core.pipeline import stalled_recording_watch, transition
    transition(jid, "processing")
    with get_session() as s:
        s.get(Job, jid).started_ts = (dt.datetime.now(dt.timezone.utc)
                                      - dt.timedelta(minutes=90))
        s.commit()
    assert stalled_recording_watch() == 0
    assert calls == []


def test_heartbeat_net_dannyh_ne_blokuiruet_watchdog(db, monkeypatch):
    # faila molotka net (hb_age=None) — decision nie menyaetsya: salvazhe vsyo
    # ravno nuzhen (fayl v opisanii sluchaya upavshego web-bot mogut net)
    monkeypatch.setenv("STALLED_RECORDING_MIN", "20")
    monkeypatch.setenv("WEBBOT_HEARTBEAT_FILE", "/tmp/net-takogo-hb-file")
    calls = _salvage_calls(monkeypatch)
    _recording_job(started_min_ago=45)
    from mnema.core.pipeline import _heartbeat_age_s, stalled_recording_watch
    assert _heartbeat_age_s() is None
    assert stalled_recording_watch() == 1
    assert len(calls) == 1
