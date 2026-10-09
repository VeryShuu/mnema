"""Подтверждение завершения записи по письму (confirm-end): API + письмо.

Redis niedostupen v testah (noop) — proveryaem sozdanie zaprosa, odnorazovost'
tokena, pis'mo s dvumya ssylkami i vnutrinniy stop-put'.
"""
import os

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
os.environ["LLM_BASE_URL"] = ""
os.environ["ASR_BASE_URL"] = ""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as c:
        yield c


def _recording_job(client) -> int:
    jid = client.post("/api/jobs", json={
        "meeting_id": "999000111", "topic": "Встреча под вопросом"}).json()["id"]
    client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    client.post(f"/api/jobs/{jid}/status", json={"status": "recording"})
    return jid


def test_confirm_end_creates_request_and_event(client):
    jid = _recording_job(client)
    r = client.post(f"/api/jobs/{jid}/confirm-end",
                    json={"reason": "нет измерений уровня после окончания"})
    assert r.status_code == 200 and r.json()["ok"] is True
    ev = client.get(f"/api/jobs/{jid}").json()["events"]
    assert any(e["event"] == "confirm_end_requested" for e in ev)


def test_confirm_end_dedup_pending(client):
    jid = _recording_job(client)
    r1 = client.post(f"/api/jobs/{jid}/confirm-end", json={})
    r2 = client.post(f"/api/jobs/{jid}/confirm-end", json={})
    assert r1.json()["request_id"] == r2.json()["request_id"]
    assert r2.json().get("already_pending") is True


def test_confirm_end_rejected_when_not_recording(client):
    jid = client.post("/api/jobs", json={"meeting_id": "999000112"}).json()["id"]
    r = client.post(f"/api/jobs/{jid}/confirm-end", json={})
    assert r.status_code == 409


def test_confirm_end_unknown_job_404(client):
    r = client.post("/api/jobs/999999/confirm-end", json={})
    assert r.status_code == 404


def test_confirm_link_confirms_once(client, monkeypatch):
    stops: list[tuple[int, str | None]] = []
    monkeypatch.setattr("mnema.core.jobs_launch.request_stop",
                        lambda jid, reason=None: stops.append((jid, reason)) or True)
    jid = _recording_job(client)
    rid = client.post(f"/api/jobs/{jid}/confirm-end", json={"reason": "r"}).json()["request_id"]

    from mnema.core.db import get_session
    from mnema.core.models import ConfirmEndRequest
    with get_session() as s:
        token = s.get(ConfirmEndRequest, rid).token

    r = client.get(f"/confirm-end/{token}")
    assert r.status_code == 200 and "подтверждено" in r.text.lower()
    assert stops == [(jid, "manual_leave")]
    with get_session() as s:
        assert s.get(ConfirmEndRequest, rid).status == "confirmed"

    r2 = client.get(f"/confirm-end/{token}")  # powtornaya ssylka — nie pwtoryaet stop
    assert r2.status_code == 200 and "уже обработан" in r2.text.lower()
    assert stops == [(jid, "manual_leave")]


def test_confirm_link_unknown_token_404(client):
    assert client.get("/confirm-end/net-takogo-tokena").status_code == 404


def test_confirm_end_notification_has_both_links_and_screenshot(
        client, tmp_path, monkeypatch):
    from pathlib import Path
    root = tmp_path / "failure_screenshots"
    root.mkdir(parents=True, exist_ok=True)
    shot = root / "job_shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    jid = _recording_job(client)
    r = client.post(f"/api/jobs/{jid}/confirm-end",
                    json={"reason": "slepoy detektor",
                          "screenshot_path": str(shot)})
    assert r.status_code == 200

    from mnema.core.db import get_session
    from mnema.core.models import Notification
    with get_session() as s:
        n = [x for x in s.query(Notification).all() if x.kind == "confirm_end"]
    assert len(n) == 1
    assert "confirm-end/" in n[0].body
    assert n[0].screenshot_path == str(shot)

    from mnema.core.notify import NotifyService
    msg = NotifyService(sender=lambda m: None).build_message(n[0])
    html = "\n".join(p.get_payload(decode=True).decode("utf-8", "ignore")
                     for p in msg.walk()
                     if p.get_content_type() == "text/html")
    assert "/confirm-end/" in html           # ssylka «Zavershit'»
    assert "cid:failure_screenshot" in html  # skrinshot inline v pis'me

