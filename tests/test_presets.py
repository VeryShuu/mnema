"""Testy predopredelennyh ssylok Zoom (CRUD + vybor pri sozdanii vstrechi)."""
import os

import pytest
from sqlalchemy import StaticPool, create_engine

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive")
os.environ.setdefault("DATA_DIR", "/tmp/test-data")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
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


def test_add_preset_appears_in_list(client):
    r = client.post("/presets/add", data={
        "title": "Planerka",
        "meeting_link": "https://us06web.zoom.us/j/123456789?pwd=secret1"},
        follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/jobs")
    assert page.status_code == 200
    assert "Planerka" in page.text


def test_add_preset_rejects_invalid_link(client):
    r = client.post("/presets/add", data={"title": "X", "meeting_link": "http://ya.ru"})
    assert r.status_code == 422


def test_add_preset_rejects_empty_title(client):
    r = client.post("/presets/add", data={"title": " ", "meeting_link": "123456789"})
    assert r.status_code == 422


def test_preset_delete(client):
    client.post("/presets/add", data={"title": "Corp", "meeting_link": "https://zoom.us/my/corp.gold?pwd=abc.1"})
    presets = client.get("/jobs").text
    assert "Corp" in presets
    from mnema.core.presets_api import list_presets
    pid = list_presets()[0].id
    r = client.post(f"/presets/{pid}/delete")
    assert r.status_code == 200
    assert "Corp" not in client.get("/jobs").text


def test_delete_missing_preset_404(client):
    assert client.post("/presets/999/delete").status_code == 404


def test_create_job_from_preset_substitutes_link_and_pwd(client):
    client.post("/presets/add", data={
        "title": "Corp",
        "meeting_link": "https://us06web.zoom.us/my/corp.gold?pwd=abc.1"})
    from mnema.core.presets_api import list_presets
    pid = list_presets()[0].id
    r = client.post("/jobs/create", data={"preset_id": str(pid), "topic": "Sovet"},
                    follow_redirects=False)
    assert r.status_code == 303
    jid = r.headers["location"].rsplit("/", 1)[-1]
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["zoom_meeting_id"] == "my/corp.gold"


def test_create_job_from_preset_uses_preset_pwd_when_link_prefilled(client):
    """applyPreset() kopiruet ssylku v formu: пароль dolzhen vzyatsya iz
    presaeta, a ne teryat'sya (vstrecha 56: pwd_ref=NULL, bot na pinesscode)."""
    client.post("/presets/add", data={
        "title": "VIP",
        "meeting_link": "https://us06web.zoom.us/my/corp.vip?pwd=pin.42"})
    from mnema.core.presets_api import list_presets
    pid = list_presets()[0].id
    r = client.post("/jobs/create",
                    data={"preset_id": str(pid), "topic": "Planerka",
                          "meeting_link": "https://us06web.zoom.us/my/corp.vip"},
                    follow_redirects=False)
    assert r.status_code == 303
    jid = r.headers["location"].rsplit("/", 1)[-1]
    info = client.get(f"/api/jobs/{jid}/join-info").json()
    assert info["pwd"] == "pin.42"
    assert info["join_link"] == "https://us06web.zoom.us/my/corp.vip"


def test_create_job_with_unknown_preset_redirects_with_error(client):
    r = client.post("/jobs/create", data={"preset_id": "999"}, follow_redirects=False)
    assert r.status_code == 303
    assert "error=preset" in r.headers["location"]


def test_preset_pwd_roundtrip_via_secrets_store(client):
    from mnema.core.presets_api import add_preset, get_preset_join
    p = add_preset(title="P", meeting_link="https://zoom.us/j/123456789?pwd=top")
    link, pwd = get_preset_join(p.id)
    assert pwd == "top"
    assert pwd not in link


def test_preset_manual_pwd_without_link_pwd(client):
    from mnema.core.presets_api import add_preset, get_preset_join
    p = add_preset(title="P2", meeting_link="https://zoom.us/j/123456789", pwd="manual")
    assert get_preset_join(p.id)[1] == "manual"
