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


def test_leave_job_not_found(client):
    r = client.post("/api/jobs/999999/leave")
    assert r.status_code == 404


def test_leave_rejected_for_wrong_status(client):
    jid = client.post("/api/jobs", json={"meeting_id": "321321321"}).json()["id"]
    r = client.post(f"/api/jobs/{jid}/leave")  # scheduled -> ne dolzhno razreshatsya
    assert r.status_code == 409


def test_leave_recording_queues_stop_and_logs_event(client, monkeypatch):
    from mnema.core.db import get_session
    from mnema.core.models import SessionEvent
    stops = []
    monkeypatch.setattr("mnema.core.jobs_launch.request_stop",
                        lambda job_id, reason=None: stops.append((job_id, reason)) or True)
    jid = client.post("/api/jobs", json={"meeting_id": "654654654"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    client.post(f"/api/jobs/{jid}/status", json={"status": "recording"})
    r = client.post(f"/api/jobs/{jid}/leave")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "status": "recording", "stop_queued": True}
    assert stops == [(jid, "manual_leave")]
    with get_session() as s:
        evs = s.scalars(__import__("sqlalchemy").select(SessionEvent)).all()
        assert any(e.event == "leave_requested" for e in evs)


def test_web_leave_endpoint(client, monkeypatch):
    stops = []
    monkeypatch.setattr("mnema.core.jobs_launch.request_stop",
                        lambda job_id, reason=None: stops.append(job_id) or True)
    jid = client.post("/api/jobs", json={"meeting_id": "987987987"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    r = client.post(f"/jobs/{jid}/leave")
    assert r.status_code == 200 and r.json()["stop_queued"] is True
    assert stops == [jid]


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


def test_jobs_page_renamed_and_start_now_option(client):
    r = client.get("/jobs")
    assert "Запланировать задачу" in r.text
    assert "Создать задачу" not in r.text
    assert 'name="start_now"' in r.text


def test_web_job_create_start_now_ignores_start_at(client):
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    r = client.post("/jobs/create", data={
        "meeting_link": "https://us05web.zoom.us/j/123123123",
        "topic": "Idushchaya", "start_now": "1", "start_at": future},
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/jobs/")
    jobs = client.get("/api/jobs").json()
    j = next(x for x in jobs if x["zoom_meeting_id"] == "123123123")
    started = dt.datetime.fromisoformat(j["start_at"])
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.timezone.utc)
    assert abs((started - dt.datetime.now(dt.timezone.utc)).total_seconds()) < 300


def test_web_job_create_schedule_keeps_start_at(client):
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).replace(microsecond=0)
    r = client.post("/jobs/create", data={
        "meeting_link": "https://us05web.zoom.us/j/456456456",
        "topic": "Pozzhe", "start_at": future.isoformat()},
        follow_redirects=False)
    assert r.status_code == 303
    jobs = client.get("/api/jobs").json()
    j = next(x for x in jobs if x["zoom_meeting_id"] == "456456456")
    started = dt.datetime.fromisoformat(j["start_at"]).replace(tzinfo=dt.timezone.utc)
    assert started == future


def test_web_job_create_invalid_link_redirects_with_error(client):
    r = client.post("/jobs/create", data={
        "meeting_link": "https://example.com/not-zoom",
        "topic": "Bad", "start_now": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs?error=link"
    jobs = client.get("/api/jobs").json()
    assert all(x.get("topic") != "Bad" for x in jobs)


def test_web_job_create_invalid_link_empty(client):
    r = client.post("/jobs/create", data={"meeting_link": "", "start_now": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs?error=link"


def test_web_jobs_shows_link_error_banner(client):
    r = client.get("/jobs?error=link")
    assert r.status_code == 200
    assert "Некорректная ссылка Zoom" in r.text


def test_web_job_create_plain_meeting_id_accepted(client):
    r = client.post("/jobs/create", data={
        "meeting_link": "4383506695", "start_now": "1", "topic": "OnlyId"},
        follow_redirects=False)
    assert r.status_code == 303
    jobs = client.get("/api/jobs").json()
    assert any(x["zoom_meeting_id"] == "4383506695" for x in jobs)


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


def _make_review_fixture(client):
    """Job v awaiting_review + pending review + segmenty + meeting.wav (3 s)."""
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment
    jid = client.post("/api/jobs", json={"meeting_id": "900900900"}).json()["id"]
    for st in ("launching", "recording", "processing", "awaiting_review"):
        assert client.post(f"/api/jobs/{jid}/status", json={"status": st}).status_code == 200
    import os
    wav_dir = Path(os.environ["DATA_DIR"]) / "jobs" / str(jid)
    wav_dir.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_dir / "meeting.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<51200h", *([100] * 51200)))
    with get_session() as s:
        rid_row = ReviewRequest(job_id=jid, cluster_label="SPEAKER_00", similarity=0.2)
        s.add(rid_row)
        s.add(Segment(job_id=jid, speaker_id="SPEAKER_00", diar_label="SPEAKER_00",
                      t_start=0.0, t_end=3.0, text="привет", speaker_source="unresolved",
                      needs_review=True))
        s.commit()
        return jid, rid_row.id


def test_review_create_new_makes_voice_profile(client):
    """Regres: 'Создать нового' v /reviews bez flaga create_new ne sozdaval profil'."""
    from sqlalchemy import select
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment
    jid, rid = _make_review_fixture(client)
    r = client.post(f"/api/reviews/{rid}",
                    json={"employee_name": "Иванов И. И.", "create_new": True})
    assert r.status_code == 200
    lst = client.get("/api/voice-profiles").json()
    assert any(p["employee_name"] == "Иванов И. И." for p in lst)
    with get_session() as s:
        rev = s.get(ReviewRequest, rid)
        assert rev.status == "confirmed" and rev.candidate_profile_id is not None
        seg = s.scalars(select(Segment).where(Segment.job_id == jid)).first()
        assert seg.speaker_name == "Иванов И. И."
        assert seg.needs_review is False


def test_review_create_new_embeds_cluster(client):
    """Novyĭ profil' poluchaet embedding klastera, inache on nikogda ne smatsitsya."""
    from sqlalchemy import select
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    jid, rid = _make_review_fixture(client)
    r = client.post(f"/api/reviews/{rid}",
                    json={"employee_name": "Петрова А. П.", "create_new": True})
    assert r.status_code == 200
    with get_session() as s:
        v = s.scalars(select(VoiceProfile).where(
            VoiceProfile.employee_name == "Петрова А. П.")).first()
        assert v is not None
        assert v.embedding, "embedding klastera ne vypolsen"


def test_review_name_only_without_create_new_makes_no_profile(client):
    """Staroe povedenie: bez create_new imenuetsegmty, no profil' v reestr ne popadaet."""
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    from sqlalchemy import select
    before = len(client.get("/api/voice-profiles").json())
    jid, rid = _make_review_fixture(client)
    r = client.post(f"/api/reviews/{rid}", json={"employee_name": "Сидоров В. В."})
    assert r.status_code == 200
    assert len(client.get("/api/voice-profiles").json()) == before
    with get_session() as s:
        assert s.scalars(select(VoiceProfile).where(
            VoiceProfile.employee_name == "Сидоров В. В.")).first() is None


def test_review_create_new_reuses_profile_with_same_name(client):
    """Povtornoe 'Создать novo' s tem zhe FIO ne sozdaet dubl v reestre."""
    jid1, rid1 = _make_review_fixture(client)
    client.post(f"/api/reviews/{rid1}",
                json={"employee_name": "Дубль Д. Д.", "create_new": True})
    cnt1 = sum(1 for p in client.get("/api/voice-profiles").json()
               if p["employee_name"] == "Дубль Д. Д.")
    jid2, rid2 = _make_review_fixture(client)
    client.post(f"/api/reviews/{rid2}",
                json={"employee_name": "Дубль Д. Д.", "create_new": True})
    cnt2 = sum(1 for p in client.get("/api/voice-profiles").json()
               if p["employee_name"] == "Дубль Д. Д.")
    assert cnt1 == 1 and cnt2 == 1
