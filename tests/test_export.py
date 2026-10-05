"""Vygruzka v samba-zerkalo (mnema/core/export.py).

In-memory SQLite; EXPORT_DIR/DATA_DIR — cherez monkeypatch na tmp_path.
Real'noy seti net: obychnaya zapisi v lokal'nyy katalog (transport — hostovyy
CIFS-maunt, ne predmet etikh testov).
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, select, StaticPool

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
os.environ.setdefault("APP_TZ", "Europe/Moscow")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    import mnema.core.models  # noqa: F401  registratsiya tablits
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("EXPORT_DIR", raising=False)
    monkeypatch.setenv("APP_TZ", "Europe/Moscow")
    return tmp_path


def _mkjob(topic="Planerka", ended_ts=None, status="done", export_json=None):
    from mnema.core.db import get_session
    from mnema.core.models import Job
    with get_session() as s:
        j = Job(topic=topic, status=status, ended_ts=ended_ts,
                export_json=export_json)
        s.add(j)
        s.commit()
        return j.id


def _mk_audio(job_id: int, kind: str = "mp3", content: bytes = b"ID3audio"):
    from mnema.worker.tasks import _job_paths
    p = _job_paths(job_id)[kind]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _events(job_id: int):
    from mnema.core.db import get_session
    from mnema.core.models import SessionEvent
    with get_session() as s:
        return s.scalars(select(SessionEvent).where(
            SessionEvent.job_id == job_id)).all()


def _job_export(job_id: int):
    from mnema.core.db import get_session
    from mnema.core.models import Job
    with get_session() as s:
        return s.get(Job, job_id).export_json


DAY_UTC = dt.datetime(2026, 9, 30, 20, 30, tzinfo=dt.timezone.utc)  # 23:30 MSK


# ------------------------------------------------------------------ konfiguratsiya
def test_export_disabled_without_env(env, monkeypatch):
    """EXPORT_DIR ne zadan → no-op: False, bez sobytij, attempts net."""
    from mnema.core import export
    jid = _mkjob(ended_ts=DAY_UTC)
    _mk_audio(jid)
    assert export.export_recording(jid) is False
    assert _events(jid) == []
    assert _job_export(jid) is None


def test_export_configured_requires_existing_writable_dir(env, monkeypatch,
                                                          tmp_path):
    from mnema.core import export
    assert export.export_configured() is False
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "ne-sushchestvuet"))
    assert export.export_configured() is False
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    assert export.export_configured() is True


# ------------------------------------------------------------------ sanitize
def test_sanitize_removes_forbidden_chars():
    from mnema.core.export import sanitize_topic
    out = sanitize_topic("Встреча / планирование: Q3 <v2>", 7)
    assert out == "Встреча - планирование- Q3 -v2-"
    for ch in '<>:"/\\|?*':
        assert ch not in out


def test_sanitize_empty_topic_falls_back_to_job_id():
    from mnema.core.export import sanitize_topic
    assert sanitize_topic("", 12) == "job12"
    assert sanitize_topic(None, 12) == "job12"
    assert sanitize_topic("///:::", 12) == "job12"


def test_sanitize_truncates_by_utf8_bytes():
    from mnema.core.export import MAX_STEM_BYTES, sanitize_topic
    out = sanitize_topic("Й" * 300, 1)   # 2 bayta na bukvu
    assert len(out.encode("utf-8")) <= MAX_STEM_BYTES


# ------------------------------------------------------------------ papka dnya
@pytest.mark.parametrize("utc,expected", [
    ((2026, 9, 30, 20, 30), "30.09.26"),   # 23:30 MSK togo zhe dnya
    ((2026, 9, 30, 21, 30), "01.10.26"),   # 00:30 MSK sleduyushchego dnya
])
def test_day_dir_uses_local_tz(env, monkeypatch, tmp_path, utc, expected):
    from mnema.core.export import export_day_dir
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    d = export_day_dir(dt.datetime(*utc, tzinfo=dt.timezone.utc))
    assert d.name == expected


# ------------------------------------------------------------------ happy path
def test_export_recording_happy_path(env, monkeypatch, tmp_path):
    from mnema.core import export
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    jid = _mkjob(topic="Planerka", ended_ts=DAY_UTC)
    src = _mk_audio(jid)
    assert export.export_recording(jid) is True
    dst = tmp_path / "share" / "30.09.26" / "Planerka.mp3"
    assert dst.is_file() and dst.read_bytes() == src.read_bytes()
    ej = _job_export(jid)
    assert ej["audio"] == str(dst) and ej["stem"] == "Planerka"
    assert any(e.event == "audio_exported" for e in _events(jid))


def test_export_fallback_to_wav(env, monkeypatch, tmp_path):
    from mnema.core import export
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    jid = _mkjob(topic="Bez mp3", ended_ts=DAY_UTC)
    _mk_audio(jid, kind="wav")
    assert export.export_recording(jid) is True
    assert (tmp_path / "share" / "30.09.26" / "Bez mp3.wav").is_file()


def test_export_collision_second_job_gets_suffix(env, monkeypatch, tmp_path):
    from mnema.core import export
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    j1 = _mkjob(topic="Sinhr", ended_ts=DAY_UTC)
    j2 = _mkjob(topic="Sinhr", ended_ts=DAY_UTC)
    _mk_audio(j1)
    _mk_audio(j2)
    assert export.export_recording(j1) is True
    assert export.export_recording(j2) is True
    d = tmp_path / "share" / "30.09.26"
    assert (d / "Sinhr.mp3").is_file() and (d / "Sinhr (2).mp3").is_file()


def test_export_idempotent_same_path_no_suffix(env, monkeypatch, tmp_path):
    from mnema.core import export
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    jid = _mkjob(topic="Povtor", ended_ts=DAY_UTC)
    _mk_audio(jid)
    assert export.export_recording(jid) is True
    first = _job_export(jid)["audio"]
    assert export.export_recording(jid) is True
    assert _job_export(jid)["audio"] == first
    d = tmp_path / "share" / "30.09.26"
    assert sorted(f.name for f in d.iterdir()) == ["Povtor.mp3"]


# ------------------------------------------------------------------ protokoly
def _mk_protocol(job_id: int, tmp_path, body="# Prot\n"):
    from pathlib import Path
    from mnema.core.db import get_session
    from mnema.core.models import Protocol
    md = tmp_path / "archive" / "protocol.md"
    docx = tmp_path / "archive" / "protocol.docx"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(body, encoding="utf-8")
    docx.write_bytes(b"PK fake docx")
    with get_session() as s:
        s.add(Protocol(job_id=job_id, md_path=str(md), docx_path=str(docx),
                       json_data={}))
        s.commit()
    return md, docx


def test_export_protocol_files_written_and_overwritten(env, monkeypatch,
                                                       tmp_path):
    from mnema.core import export
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    jid = _mkjob(topic="Soveshchaniye", ended_ts=DAY_UTC)
    _mk_audio(jid)
    export.export_recording(jid)
    md, _ = _mk_protocol(jid, tmp_path)
    assert export.export_protocol_files(jid) is True
    d = tmp_path / "share" / "30.09.26"
    out_md = d / "Soveshchaniye - протокол.md"
    out_docx = d / "Soveshchaniye - протокол.docx"
    assert out_md.is_file() and out_docx.is_file()
    assert _job_export(jid)["protocol_md"] == str(out_md)
    md.write_text("# Prot v2\n", encoding="utf-8")
    assert export.export_protocol_files(jid) is True
    assert out_md.read_text(encoding="utf-8") == "# Prot v2\n"
    assert _job_export(jid)["protocol_md"] == str(out_md)  # tot zhe put'
    assert sorted(f.name for f in d.iterdir()
                  if "протокол" in f.name) == ["Soveshchaniye - протокол.docx",
                                               "Soveshchaniye - протокол.md"]


def test_refresh_protocol_calls_export(env, monkeypatch, tmp_path):
    """refresh_protocol (perepisaet md/docx) za soboy sygaet v zerkalo."""
    from mnema.core import export
    from mnema.core.db import get_session
    from mnema.core.models import Protocol
    from mnema.core.protocol_md import refresh_protocol
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    jid = _mkjob(topic="Refresh", ended_ts=DAY_UTC)
    _mk_audio(jid)
    export.export_recording(jid)
    md, _ = _mk_protocol(jid, tmp_path)
    with get_session() as s:
        pid = s.scalars(select(Protocol).where(
            Protocol.job_id == jid)).first().id
    md.write_text("# Refreshed\n", encoding="utf-8")
    assert refresh_protocol(pid) is True
    out_md = tmp_path / "share" / "30.09.26" / "Refresh - протокол.md"
    assert out_md.is_file()
    # refresh_protocol perepisyvaet md lokal'no po dannym BDD (render_markdown),
    # zerkalo dolzhno soderzhat' tu zhe svezhuyu kopiyu
    assert out_md.read_text(encoding="utf-8") == md.read_text(encoding="utf-8")
    assert "# Протокол: Refresh" in out_md.read_text(encoding="utf-8")
    assert out_md.read_text(encoding="utf-8") != "# Prot\n"  # ne staryy fail


# ------------------------------------------------------------------ oshibki
def test_export_error_sets_event_attempts_and_no_raise(env, monkeypatch,
                                                       tmp_path):
    """Den'-katalog nevozmozhno sozdat' (fayl na puti) → False + export_error +
    attempts++, isklyucheniye naruzhu ne letit."""
    from mnema.core import export
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setenv("EXPORT_DIR", str(share))
    jid = _mkjob(topic="Bol'", ended_ts=DAY_UTC)
    _mk_audio(jid)
    (share / "30.09.26").write_text("ne katalog")  # blokiruet mkdir
    assert export.export_recording(jid) is False
    assert any(e.event == "export_error" for e in _events(jid))
    assert _job_export(jid)["attempts"] == 1


# ------------------------------------------------------------------ retrai
def test_retry_fixes_underexported_job(env, monkeypatch, tmp_path):
    from mnema.core import export
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    jid = _mkjob(topic="Dovygrunt", ended_ts=DAY_UTC, status="done")
    _mk_audio(jid)
    assert export.retry_pending_exports() == 1
    assert (tmp_path / "share" / "30.09.26" / "Dovygrunt.mp3").is_file()
    assert export.retry_pending_exports() == 0  # uzhe polno — nichego delat'


def test_retry_covers_protocols(env, monkeypatch, tmp_path):
    from mnema.core import export
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    jid = _mkjob(topic="Proty", ended_ts=DAY_UTC, status="awaiting_review")
    _mk_protocol(jid, tmp_path)
    assert export.retry_pending_exports() == 1
    d = tmp_path / "share" / "30.09.26"
    assert (d / "Proty - протокол.md").is_file()
    assert (d / "Proty - протокол.docx").is_file()


def test_retry_skips_exhausted_and_notifies(env, monkeypatch, tmp_path):
    from mnema.core import export, notify
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    jid = _mkjob(topic="Otbros", ended_ts=DAY_UTC, status="processing",
                 export_json={"attempts": 6})
    _mk_audio(jid)
    enqueued: list[str] = []
    monkeypatch.setattr(notify.NotifyService, "enqueue",
                        lambda self, kind, subject, body, dedup_key=None, data=None:
                        enqueued.append(dedup_key))
    assert export.retry_pending_exports() == 0
    assert _job_export(jid)["attempts"] == 6  # popytok ne bylo
    assert enqueued == [f"export_failed:{jid}"]
    assert not (tmp_path / "share" / "30.09.26").exists()


def test_retry_noop_when_disabled(env, monkeypatch, tmp_path):
    from mnema.core import export
    jid = _mkjob(ended_ts=DAY_UTC)
    _mk_audio(jid)
    assert export.retry_pending_exports() == 0
