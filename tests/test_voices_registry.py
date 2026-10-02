"""Testy pererabotki reestra golosov (/voices): proslushivanie etalonov i
klasteov, pravka profilya, ruchnaya pereproverka raspoznavaniya.

Redis niedostupen (direct mode) — enqueue monkeypatch-atsya. Pyannote niedostupen —
embedding-logika proveryaetsya s monkeypatch-backendom.
"""
import datetime as dt
import os
import struct
import wave
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine, select

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive-voices")
os.environ.setdefault("DATA_DIR", "/tmp/test-data-voices")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-voices.sqlite")
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


def _mk_profile(client, name="Ivanov I.I.", **kw):
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = VoiceProfile(employee_name=name,
                         consent_ts=dt.datetime.now(dt.timezone.utc), **kw)
        s.add(v)
        s.commit()
        s.refresh(v)
        return v.id


def _mk_sample_file(client, vid, name="sample_1.webm"):
    """Fayl etalona na diske + zapis' v profile.sample_paths."""
    d = Path(os.environ["DATA_DIR"]) / "voice_samples" / str(vid)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"webm-fake-audio-bytes")
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        v.sample_paths = list(v.sample_paths or []) + [str(p)]
        s.commit()
    return p


# ---- GET /api/voice-profiles/{vid}/samples ---------------------------------
def test_samples_list_empty(client):
    vid = _mk_profile(client)
    assert client.get(f"/api/voice-profiles/{vid}/samples").json() == []


def test_samples_list_shape_and_url(client):
    vid = _mk_profile(client)
    _mk_sample_file(client, vid)
    out = client.get(f"/api/voice-profiles/{vid}/samples").json()
    assert len(out) == 1
    assert out[0]["exists"] is True
    assert out[0]["url"] == f"/api/voice-profiles/{vid}/samples/0"


def test_samples_list_missing_file_marked(client):
    vid = _mk_profile(client)
    p = _mk_sample_file(client, vid)
    p.unlink()
    out = client.get(f"/api/voice-profiles/{vid}/samples").json()
    assert out[0]["exists"] is False


def test_samples_list_404_unknown_profile(client):
    assert client.get("/api/voice-profiles/99999/samples").status_code == 404


# ---- GET /api/voice-profiles/{vid}/samples/{idx} ---------------------------
def test_sample_file_serves_audio(client):
    vid = _mk_profile(client)
    _mk_sample_file(client, vid)
    r = client.get(f"/api/voice-profiles/{vid}/samples/0")
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == b"webm-fake-audio-bytes"


def test_sample_file_range_request(client):
    vid = _mk_profile(client)
    _mk_sample_file(client, vid)
    r = client.get(f"/api/voice-profiles/{vid}/samples/0",
                   headers={"range": "bytes=0-3"})
    assert r.status_code == 206
    assert r.content == b"webm"


def test_sample_file_404_bad_index(client):
    vid = _mk_profile(client)
    assert client.get(f"/api/voice-profiles/{vid}/samples/0").status_code == 404


def test_sample_file_404_missing_on_disk(client):
    vid = _mk_profile(client)
    p = _mk_sample_file(client, vid)
    p.unlink()
    assert client.get(f"/api/voice-profiles/{vid}/samples/0").status_code == 404


# ---- DELETE /api/voice-profiles/{vid}/samples/{idx} -------------------------
def test_delete_sample_removes_file_and_enqueues_reembed(client, monkeypatch):
    calls = []
    monkeypatch.setattr("mnema.core.queue.enqueue_job",
                        lambda name, func, *a, **kw: calls.append((name, func, a)) or None)
    vid = _mk_profile(client)
    p1 = _mk_sample_file(client, vid, "a.webm")
    p2 = _mk_sample_file(client, vid, "b.webm")
    r = client.delete(f"/api/voice-profiles/{vid}/samples/0")
    assert r.status_code == 200
    assert r.json()["samples"] == 1
    assert not p1.exists() and p2.exists()
    assert calls and "run_profile_reembed" in str(calls[0][1])
    assert calls[0][2] == (vid,)


def test_delete_sample_bad_index_404(client):
    vid = _mk_profile(client)
    assert client.delete(f"/api/voice-profiles/{vid}/samples/5").status_code == 404


def test_delete_sample_404_unknown_profile(client):
    assert client.delete("/api/voice-profiles/99999/samples/0").status_code == 404


# ---- PATCH /api/voice-profiles/{vid} ----------------------------------------
def _mk_confirmed_cluster(client, name):
    """Profil' + podtverzhdennaya review s imenovannymi segmentami + Protocol."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, Protocol, ReviewRequest, Segment, VoiceProfile
    with get_session() as s:
        v = VoiceProfile(employee_name=name,
                         consent_ts=dt.datetime.now(dt.timezone.utc))
        s.add(v)
        s.commit()
        vid, = (v.id,)
    jid = client.post("/api/jobs", json={"meeting_id": "510510510"}).json()["id"]
    for st in ("launching", "recording", "processing", "done"):
        client.post(f"/api/jobs/{jid}/status", json={"status": st})
    with get_session() as s:
        s.add(ReviewRequest(job_id=jid, cluster_label="SPEAKER_00",
                            candidate_profile_id=vid, status="confirmed",
                            answered_by="web"))
        s.add(Segment(job_id=jid, speaker_id="SPEAKER_00", diar_label="SPEAKER_00",
                      t_start=0.0, t_end=5.0, text="replika",
                      speaker_name=name, speaker_source="voiceprint"))
        s.add(Protocol(job_id=jid, json_data={"topic": "T"}))
        s.commit()
    return vid, jid


def test_patch_profile_renames(client):
    vid = _mk_profile(client, "Staryy S.S.")
    r = client.patch(f"/api/voice-profiles/{vid}",
                     json={"employee_name": "Noviy N.N.", "employee_id": "123"})
    assert r.status_code == 200
    assert r.json()["employee_name"] == "Noviy N.N."
    lst = client.get("/api/voice-profiles").json()
    p = next(x for x in lst if x["id"] == vid)
    assert p["employee_name"] == "Noviy N.N." and p["employee_id"] == "123"


def test_patch_rename_updates_segments(client):
    vid, jid = _mk_confirmed_cluster(client, "Perekhozh P.P.")
    r = client.patch(f"/api/voice-profiles/{vid}",
                     json={"employee_name": "Final'nyy F.F."})
    assert r.status_code == 200
    from mnema.core.db import get_session
    from mnema.core.models import Segment
    with get_session() as s:
        seg = s.scalars(select(Segment).where(Segment.job_id == jid)).first()
        assert seg.speaker_name == "Final'nyy F.F."


def test_patch_empty_name_rejected(client):
    vid = _mk_profile(client)
    assert client.patch(f"/api/voice-profiles/{vid}",
                        json={"employee_name": "   "}).status_code == 400


def test_patch_404_unknown_profile(client):
    assert client.patch("/api/voice-profiles/99999",
                        json={"employee_name": "X"}).status_code == 404


# ---- dolzhnost' (vozvrashchena vmesto tabel'nogo v /voices) -------------------
def test_create_profile_with_position(client):
    r = client.post("/api/voice-profiles",
                    json={"employee_name": "Petrova P.P.",
                          "position": "Nachal'nik otdela", "consent": True})
    assert r.status_code == 200
    lst = client.get("/api/voice-profiles").json()
    p = next(x for x in lst if x["id"] == r.json()["id"])
    assert p["position"] == "Nachal'nik otdela"


def test_patch_profile_sets_and_clears_position(client):
    vid = _mk_profile(client)
    r = client.patch(f"/api/voice-profiles/{vid}", json={"position": "Director"})
    assert r.status_code == 200
    p = next(x for x in client.get("/api/voice-profiles").json() if x["id"] == vid)
    assert p["position"] == "Director"
    client.patch(f"/api/voice-profiles/{vid}", json={"position": "   "})
    p = next(x for x in client.get("/api/voice-profiles").json() if x["id"] == vid)
    assert p["position"] is None


def test_patch_position_does_not_break_rename_fields(client):
    vid = _mk_profile(client, "Odin O.O.")
    client.patch(f"/api/voice-profiles/{vid}", json={"position": "Zam_directora"})
    r = client.patch(f"/api/voice-profiles/{vid}", json={"employee_name": "Dva D.D."})
    assert r.status_code == 200
    p = next(x for x in client.get("/api/voice-profiles").json() if x["id"] == vid)
    assert p["employee_name"] == "Dva D.D." and p["position"] == "Zam_directora"


def test_voices_page_shows_position_column(client):
    vid = _mk_profile(client, "Dolzhnaya D.D.", position="Glavnyy ekspert")
    r = client.get("/voices")
    assert r.status_code == 200
    assert "Должность" in r.text
    assert "Табельный" not in r.text
    assert "Glavnyy ekspert" in r.text
    assert "emp_position" in r.text


# ---- GET /api/voice-profiles/{vid}/clusters ----------------------------------
def _mk_review_row(client, vid, jid, cluster="SPEAKER_00", status="confirmed",
                   answered_by="web"):
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment
    with get_session() as s:
        r = ReviewRequest(job_id=jid, cluster_label=cluster,
                          candidate_profile_id=vid, status=status,
                          answered_by=answered_by, similarity=0.72)
        s.add(r)
        s.add(Segment(job_id=jid, speaker_id=cluster, diar_label=cluster,
                      t_start=10.0, t_end=14.0, text="tekst fragmenta",
                      speaker_source="unresolved"))
        s.commit()


def _mk_job_audio(client, jid, kind="wav", alive=True):
    from mnema.core.db import get_session
    from mnema.core.models import AudioFile
    d = Path(os.environ["DATA_DIR"]) / "jobs" / str(jid)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"meeting.{kind}"
    if alive:
        p.write_bytes(b"fakeaudio")
    with get_session() as s:
        s.add(AudioFile(job_id=jid, kind=kind, path=str(p), bytes=9))
        s.commit()


def test_clusters_empty_for_new_profile(client):
    vid = _mk_profile(client)
    assert client.get(f"/api/voice-profiles/{vid}/clusters").json() == []


def test_clusters_shape_with_audio(client):
    vid = _mk_profile(client)
    jid = client.post("/api/jobs", json={"meeting_id": "610610610",
                                         "topic": "Soveshch"}).json()["id"]
    _mk_job_audio(client, jid, "wav")
    _mk_review_row(client, vid, jid)
    out = client.get(f"/api/voice-profiles/{vid}/clusters").json()
    assert len(out) == 1
    c = out[0]
    assert c["job_id"] == jid and c["topic"] == "Soveshch"
    assert c["status"] == "confirmed" and c["has_audio"] is True
    assert c["audio_format"] == "wav"
    assert c["segments"][0]["t_start"] == 10.0


def test_clusters_pending_included_and_dead_audio(client):
    vid = _mk_profile(client)
    jid = client.post("/api/jobs", json={"meeting_id": "620620620"}).json()["id"]
    _mk_review_row(client, vid, jid, status="pending", answered_by=None)
    _mk_job_audio(client, jid, "wav", alive=False)
    out = client.get(f"/api/voice-profiles/{vid}/clusters").json()
    assert out[0]["status"] == "pending" and out[0]["has_audio"] is False


def test_clusters_404_unknown_profile(client):
    assert client.get("/api/voice-profiles/99999/clusters").status_code == 404


# ---- POST /api/voice-profiles/{vid}/reverify --------------------------------
def test_reverify_enqueues_worker_task(client, monkeypatch):
    from mnema.core import queue as queue_mod
    queue_mod._queues.clear()
    monkeypatch.setattr(queue_mod, "get_queue", lambda name: (None, "rq"))
    calls = []

    class FakeQ:
        def enqueue(self, func, *a, **kw):
            calls.append((func, a))
            return object()
    monkeypatch.setattr(queue_mod, "get_queue",
                        lambda name: (FakeQ(), "rq"))
    vid = _mk_profile(client)
    r = client.post(f"/api/voice-profiles/{vid}/reverify")
    assert r.status_code == 200
    assert r.json()["queued"] is True
    assert "run_reverify_profile" in str(calls[0][0])
    assert calls[0][1] == (vid,)


def test_reverify_reports_no_queue(client, monkeypatch):
    monkeypatch.setattr("mnema.core.queue.enqueue_job",
                        lambda *a, **kw: None)
    vid = _mk_profile(client)
    r = client.post(f"/api/voice-profiles/{vid}/reverify")
    assert r.status_code == 200 and r.json()["queued"] is False


def test_reverify_404_unknown_profile(client):
    assert client.post("/api/voice-profiles/99999/reverify").status_code == 404


# ---- /voices page -------------------------------------------------------------
def test_voices_page_has_new_controls(client):
    vid = _mk_profile(client, "Kontrol' K.K.")
    r = client.get("/voices")
    assert r.status_code == 200
    assert "Кластеры и эталоны" in r.text
    assert "Перепроверить" in r.text
    assert "editProfile(" in r.text and "reverify(" in r.text
    assert "Kontrol&#39; K.K." in r.text or "Kontrol' K.K." in r.text


def test_stats_text_renders_human_readable(client):
    import mnema.api.main as m
    assert m.stats_text(None) == "—"
    s = m.stats_text({"confirmed": 4,
                      "reverify_done": {"ts": "2026-09-29T07:00:00+00:00",
                                        "detail": "etalon: 1 + 2"}})
    assert "подтверждено: 4" in s and "reverify_done" in s


# ---- worker: run_profile_reembed / run_reverify_profile -----------------------
def _wf():
    """Worker-test env: in-memory db v stile test_speaker_backfill."""
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)


def test_run_profile_reembed_skips_without_backend(monkeypatch):
    _wf()
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: None)
    from mnema.worker.tasks import run_profile_reembed
    run_profile_reembed(1)  # bez issklyucheniy


def test_recompute_profile_embedding_from_sample(db_voice, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker.compute_embedding_for_file",
                        lambda path: [0.0, 1.0])
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = VoiceProfile(employee_name="Reembed Re.Re.")
        s.add(v)
        s.commit()
        vid = v.id
    d = Path(os.environ["DATA_DIR"]) / "voice_samples" / str(vid)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "a.webm"
    p.write_bytes(b"x")
    with get_session() as s:
        v = s.get(VoiceProfile, vid)
        v.sample_paths = [str(p)]
        s.commit()
    from mnema.core.speaker_backfill import recompute_profile_embedding
    detail = recompute_profile_embedding(vid)
    assert "1 zapisey" in detail
    with get_session() as s:
        assert s.get(VoiceProfile, vid).embedding == [0.0, 1.0]


@pytest.fixture()
def db_voice(monkeypatch, tmp_path):
    _wf()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def test_reverify_profile_skips_without_backend(monkeypatch):
    _wf()
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: None)
    events = []
    monkeypatch.setattr("mnema.worker.tasks._profile_event",
                        lambda *a: events.append(a))
    from mnema.worker.tasks import run_reverify_profile
    run_reverify_profile(1)
    assert events == []  # no backend — nichego ne delaem


def test_reverify_profile_flow(db_voice, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker_backfill.recompute_profile_embedding",
                        lambda pid: "etalon: 0 zapisey + 2 klasteov")
    monkeypatch.setattr("mnema.core.speaker_backfill.profile_review_jobs",
                        lambda pid, max_jobs=None: [77, 78])
    rematched = []
    monkeypatch.setattr("mnema.core.speaker_backfill.rematch_job",
                        lambda job_id: rematched.append(job_id))
    events = []

    def fake_event(pid, ev, detail):
        events.append((pid, ev, detail))
    monkeypatch.setattr("mnema.worker.tasks._profile_event", fake_event)
    from mnema.worker.tasks import run_reverify_profile
    run_reverify_profile(5)
    assert rematched == [77, 78]
    assert events and events[0][1] == "reverify_done"
    assert "2/2" in events[0][2]


def test_reverify_profile_failure_records_event(db_voice, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")

    def boom(pid):
        raise RuntimeError("disk sgoriel")
    monkeypatch.setattr("mnema.core.speaker_backfill.recompute_profile_embedding", boom)
    events = []
    monkeypatch.setattr("mnema.worker.tasks._profile_event",
                        lambda pid, ev, detail: events.append((pid, ev, detail)))
    from mnema.worker.tasks import run_reverify_profile
    run_reverify_profile(9)
    assert events == [(9, "reverify_failed", "disk sgoriel")]


def test_profile_review_jobs_dedupes(db_voice):
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, VoiceProfile
    with get_session() as s:
        v = VoiceProfile(employee_name="Jobs J.J.")
        s.add(v)
        s.commit()
        vid = v.id
        s.add_all([
            ReviewRequest(job_id=1, cluster_label="A", candidate_profile_id=vid,
                          status="confirmed"),
            ReviewRequest(job_id=1, cluster_label="B", candidate_profile_id=vid,
                          status="confirmed"),
            ReviewRequest(job_id=2, cluster_label="C", candidate_profile_id=vid,
                          status="pending"),
        ])
        s.commit()
    from mnema.core.speaker_backfill import profile_review_jobs
    assert profile_review_jobs(vid) == [1]


def test_profile_event_writes_match_stats(db_voice):
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = VoiceProfile(employee_name="Stats S.S.")
        s.add(v)
        s.commit()
        vid = v.id
    from mnema.worker.tasks import _profile_event
    _profile_event(vid, "reverify_done", "ok")
    with get_session() as s:
        st = s.get(VoiceProfile, vid).match_stats
        assert st["reverify_done"]["detail"] == "ok"
