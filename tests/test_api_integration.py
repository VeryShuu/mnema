"""Integracionnye testy api + konveyera cherez TestClient (FF-21, 22, 34, 41).

Redis niedostupen v testah — enqueue padayet v no-op (queue mode=direct);
statusy menyaemsya cherez vnutrennie POST (kak web-bot).
"""
import datetime as dt
import os
import wave
import struct
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive")
os.environ.setdefault("DATA_DIR", "/tmp/test-data")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # zavedomo niet
# capability: pustye => not_configured (bez live-probe)
os.environ["LLM_BASE_URL"] = ""
os.environ["ASR_BASE_URL"] = ""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as c:
        yield c


def test_health_shape(client):
    r = client.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    assert j["capabilities"]["llm"]["state"] == "not_configured"
    assert j["capabilities"]["asr"]["state"] == "not_configured"
    assert "zoom_session" in j and "queue" in j


def test_create_job_from_invite_link(client):
    r = client.post("/api/jobs", json={
        "meeting_link": "https://us02web.zoom.us/j/9814832357?pwd=abc",
        "topic": "Planerka"})
    assert r.status_code == 200
    jid = r.json()["id"]
    assert r.json()["status"] == "scheduled"
    g = client.get(f"/api/jobs/{jid}").json()
    assert g["zoom_meeting_id"] == "9814832357"
    # pwd НЕ v public otvete
    assert "pwd" not in g
    # no v nutrennem join-info
    ji = client.get(f"/api/jobs/{jid}/join-info").json()
    assert ji["meeting_id"] == "9814832357" and ji["pwd"] == "abc"


def test_job_lifecycle_events(client):
    jid = client.post("/api/jobs", json={
        "meeting_id": "123456789", "topic": "T"}).json()["id"]
    # web-bot: launching -> recording
    assert client.post(f"/api/jobs/{jid}/status", json={"status": "launching"}).status_code == 200
    assert client.post(f"/api/jobs/{jid}/status", json={"status": "recording"}).status_code == 200
    # sobytie
    client.post(f"/api/jobs/{jid}/events", json={"event": "joined", "detail": "ok"})
    g = client.get(f"/api/jobs/{jid}").json()
    assert any(e["event"] == "joined" for e in g["events"])


def test_invalid_status_transition_rejected(client):
    jid = client.post("/api/jobs", json={"meeting_id": "222333444"}).json()["id"]
    r = client.post(f"/api/jobs/{jid}/status", json={"status": "done"})
    assert r.status_code >= 400  # scheduled -> done nie dopustimo


def test_cancel_scheduled(client):
    jid = client.post("/api/jobs", json={"meeting_id": "555666777"}).json()["id"]
    r = client.delete(f"/api/jobs/{jid}")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


def test_health_bad_request_bad_format(client):
    # sozdaem job s audio bez fila -> 404 na razdache
    r = client.get("/api/jobs/999999/audio")
    assert r.status_code == 404


def _make_wav(path: Path, seconds: float = 2.0, sr: int = 16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        frames = struct.pack(f"<{int(sr*seconds)}h", *([100] * int(sr*seconds)))
        w.writeframes(frames)


def test_audio_range_request(tmp_path, monkeypatch):
    from mnema.core.db import Base, set_engine_for_test
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as c:
        # hand-in a job + wav + protocol to hit audio route via job
        jid = c.post("/api/jobs", json={"meeting_id": "700700700"}).json()["id"]
        wavfile = tmp_path / "meeting.wav"
        _make_wav(wavfile)
        from mnema.core.db import get_session
        from mnema.core.models import AudioFile, Job
        with get_session() as s:
            s.add(AudioFile(job_id=jid, kind="mp3", path=str(wavfile), bytes=wavfile.stat().st_size))
            s.commit()
        # plain
        r = c.get(f"/api/jobs/{jid}/audio?format=mp3")
        assert r.status_code == 200
        # range
        r2 = c.get(f"/api/jobs/{jid}/audio?format=mp3", headers={"range": "bytes=0-15"})
        assert r2.status_code == 206
        assert r2.headers["content-range"].startswith("bytes 0-15/")
        assert len(r2.content) == 16


def test_ics_import_creates_jobs(client):
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1@x
DTSTART:20261001T100000Z
DTEND:20261001T110000Z
SUMMARY:Sprint review
DESCRIPTION:Join: https://us05web.zoom.us/j/999888777?pwd=zz9
END:VEVENT
END:VCALENDAR"""
    r = client.post("/jobs/import-ics", files={"file": ("x.ics", ics, "text/calendar")})
    assert r.status_code == 200 and r.json()["imported"] == 1
    jobs = client.get("/api/jobs").json()
    assert any(j["zoom_meeting_id"] == "999888777" for j in jobs)


def test_voice_profile_gate_consent(client):
    # bez soglasiya -> 400
    r = client.post("/api/voice-profiles", json={"employee_name": "X", "consent": False})
    assert r.status_code == 400
    r2 = client.post("/api/voice-profiles", json={"employee_name": "X", "consent": True})
    assert r2.status_code == 200
    vid = r2.json()["id"]
    lst = client.get("/api/voice-profiles").json()
    assert any(v["id"] == vid for v in lst)


def test_web_pages_render(client):
    for path in ["/", "/jobs", "/protocols", "/reviews", "/voices"]:
        r = client.get(path)
        assert r.status_code == 200, path


def test_reviews_page_with_pending(client):
    """Regres: .limit() na scalars lomali stranicy /reviews pri nepustoich ochenereyah."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, ReviewRequest, Segment
    jid = client.post("/api/jobs", json={"meeting_id": "810810810"}).json()["id"]
    with get_session() as s:
        s.add(ReviewRequest(job_id=jid, cluster_label="SPEAKER_UNKNOWN", similarity=0.51))
        s.add(Segment(job_id=jid, speaker_id="SPEAKER_UNKNOWN", diar_label="SPEAKER_UNKNOWN",
                      t_start=0.0, t_end=2.0, text="test", speaker_source="unresolved",
                      needs_review=True))
        s.commit()
    r = client.get("/reviews")
    assert r.status_code == 200
    assert "SPEAKER_UNKNOWN" in r.text
    api = client.get("/api/reviews?status=pending").json()
    assert api and api[0]["cluster_label"] == "SPEAKER_UNKNOWN"
