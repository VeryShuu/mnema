"""REST-kontrakt dlya portal-integracii: external_id-idempotentnost,
X-API-Key, PATCH /api/jobs, end_at + proizvodnyy end_at v join-info.

Dopolnyaet test_api_integration: novye vozmozhnosti iz PR portal-integracii.
Redis nedostupen v testah — enqueue padaet v no-op (queue mode=direct).
"""
import datetime as dt
import os

import pytest
from sqlalchemy import StaticPool, create_engine

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

LINK = "https://us02web.zoom.us/j/9814832357?pwd=abc"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.delenv("API_TOKEN", raising=False)
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    with TestClient(app) as c:
        yield c


def test_external_id_idempotent_create(client):
    body = {"meeting_link": LINK, "topic": "Planerka",
            "external_id": "portal:b1:r1:1"}
    r1 = client.post("/api/jobs", json=body)
    assert r1.status_code == 200
    j1 = r1.json()
    assert j1["existing"] is False

    # povtor s tem zhe klyuchom: tot zhe id, bez dublya i bez redaktirovaniya
    r2 = client.post("/api/jobs", json={**body, "topic": "Drugaya tema"})
    j2 = r2.json()
    assert j2["id"] == j1["id"]
    assert j2["existing"] is True
    g = client.get(f"/api/jobs/{j1['id']}").json()
    assert g["topic"] == "Planerka"
    assert g["external_id"] == "portal:b1:r1:1"

    # drugoy klyuch — novaya vstrecha; spisok nesoet external_id
    r3 = client.post("/api/jobs", json={**body, "external_id": "portal:b2:r1:1"})
    assert r3.json()["id"] != j1["id"]
    assert any(j["external_id"] == "portal:b1:r1:1"
               for j in client.get("/api/jobs").json())


def test_create_without_external_id(client):
    r = client.post("/api/jobs", json={"meeting_link": LINK})
    assert r.status_code == 200
    assert r.json()["existing"] is False


def test_api_key_enforced_when_set(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "tok-123")
    assert client.post("/api/jobs",
                       json={"meeting_link": LINK}).status_code == 401
    assert client.post("/api/jobs", json={"meeting_link": LINK},
                       headers={"X-API-Key": "wrong"}).status_code == 401
    r = client.post("/api/jobs", json={"meeting_link": LINK},
                    headers={"X-API-Key": "tok-123"})
    assert r.status_code == 200
    jid = r.json()["id"]

    # vse upravlencheskie endpointy pod ohranoy
    assert client.get("/api/jobs").status_code == 401
    assert client.get(f"/api/jobs/{jid}").status_code == 401
    assert client.patch(f"/api/jobs/{jid}",
                        json={"topic": "X"}).status_code == 401
    assert client.delete(f"/api/jobs/{jid}").status_code == 401

    # sluzhebnye endpointy web-bot ne ohvacheny (kontrakt: perimetr, ne token)
    assert client.get(f"/api/jobs/{jid}/join-info").status_code == 200
    assert client.post(f"/api/jobs/{jid}/events",
                       json={"event": "probe"}).status_code == 200


def test_api_key_unset_open(client):
    # net API_TOKEN => kak ran'she, bez auth (setevoy perimetr)
    assert client.post("/api/jobs",
                       json={"meeting_link": LINK}).status_code == 200


def test_patch_scheduled_job(client):
    jid = client.post("/api/jobs", json={
        "meeting_link": LINK, "topic": "Planerka",
        "start_at": dt.datetime(2026, 11, 15, 10, 0,
                                tzinfo=dt.timezone.utc).isoformat(),
    }).json()["id"]
    r = client.patch(f"/api/jobs/{jid}", json={
        "topic": "Planerka (perenos)",
        "start_at": dt.datetime(2026, 11, 15, 11, 0,
                                tzinfo=dt.timezone.utc).isoformat(),
    })
    assert r.status_code == 200
    g = client.get(f"/api/jobs/{jid}").json()
    assert g["topic"] == "Planerka (perenos)"
    assert g["start_at"].startswith("2026-11-15T11:00")
    # ne peredannoe end_at — bez izmeneniya (ostaetsya null)
    assert g["end_at"] is None


def test_patch_validation_and_missing(client):
    jid = client.post("/api/jobs", json={"meeting_link": LINK}).json()["id"]
    assert client.patch(f"/api/jobs/{jid}",
                        json={"topic": "   "}).status_code == 422
    assert client.patch(f"/api/jobs/{jid}",
                        json={"meeting_link": "not-a-zoom-link"}).status_code == 422
    assert client.patch("/api/jobs/999999",
                        json={"topic": "X"}).status_code == 404


def test_patch_conflict_after_start(client):
    jid = client.post("/api/jobs", json={"meeting_link": LINK}).json()["id"]
    s = client.post(f"/api/jobs/{jid}/status", json={"status": "launching"})
    assert s.status_code == 200
    r = client.patch(f"/api/jobs/{jid}", json={"topic": "Pozdno"})
    assert r.status_code == 409


def test_end_at_explicit_and_derived_in_join_info(client):
    # yavnyy end_at sohranyaetsya i vozvrashaetsya v join-info
    jid = client.post("/api/jobs", json={
        "meeting_link": LINK,
        "start_at": dt.datetime(2026, 11, 15, 10, 0,
                                tzinfo=dt.timezone.utc).isoformat(),
        "end_at": dt.datetime(2026, 11, 15, 11, 30,
                              tzinfo=dt.timezone.utc).isoformat(),
    }).json()["id"]
    ji = client.get(f"/api/jobs/{jid}/join-info").json()
    assert ji["end_at"].startswith("2026-11-15T11:30")

    # bez end_at, no s duration_max_min join-info vyvodit end_at = start + duration
    jid2 = client.post("/api/jobs", json={
        "meeting_link": LINK,
        "start_at": dt.datetime(2026, 11, 15, 12, 0,
                                tzinfo=dt.timezone.utc).isoformat(),
        "duration_max_min": 90,
    }).json()["id"]
    ji2 = client.get(f"/api/jobs/{jid2}/join-info").json()
    assert ji2["end_at"].startswith("2026-11-15T13:30")
