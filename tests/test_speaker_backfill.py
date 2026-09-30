"""Testy backfill embeddingtonov iz podtverzhdennykh klasterov + avtomaticheskogo
zakrytiya review pri verdict=auto.

Pyannote v testah niedostupen zhivom — embedding-i i matsching monkeypatch-atsya;
backend-to check proveryaetsya otdel'no.
"""
import datetime as dt
import os

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive-backfill")
os.environ.setdefault("DATA_DIR", "/tmp/test-data-backfill")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-backfill.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # zavedomo niet
os.environ["LLM_BASE_URL"] = ""
os.environ["ASR_BASE_URL"] = ""

import pytest
from sqlalchemy import StaticPool, create_engine, select


@pytest.fixture()
def db(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    yield engine


def _add_profile(name, **kw):
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = VoiceProfile(employee_name=name,
                         consent_ts=dt.datetime.now(dt.timezone.utc), **kw)
        s.add(v)
        s.commit()
        s.refresh(v)
        return v.id


def _add_review(job_id, cluster, profile_id, status="confirmed", answered_by="web"):
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest
    with get_session() as s:
        r = ReviewRequest(job_id=job_id, cluster_label=cluster,
                          candidate_profile_id=profile_id, status=status,
                          answered_by=answered_by)
        s.add(r)
        s.commit()
        s.refresh(r)
        return r.id


# ---- mean_embedding -----------------------------------------------------------
def test_mean_embedding_unit_norm():
    from mnema.core.speaker_backfill import mean_embedding
    m = mean_embedding([[1.0, 0.0], [3.0, 0.0]])
    assert m[0] == pytest.approx(1.0)
    assert m[1] == pytest.approx(0.0)


def test_mean_embedding_normalizes_mixed_vectors():
    import math
    from mnema.core.speaker_backfill import mean_embedding
    m = mean_embedding([[1.0, 0.0], [0.0, 1.0]])
    assert math.isclose(math.sqrt(sum(x * x for x in m)), 1.0)
    assert m[0] == pytest.approx(m[1])


def test_mean_embedding_empty_returns_none():
    from mnema.core.speaker_backfill import mean_embedding
    assert mean_embedding([]) is None


# ---- profile_backfill_sources -------------------------------------------------
def test_sources_take_confirmed_clusters(db):
    pid = _add_profile("Ivanov I.I.")
    _add_review(10, "SPEAKER_00", pid)
    _add_review(11, "SPEAKER_01", pid)
    from mnema.core.speaker_backfill import profile_backfill_sources
    src = profile_backfill_sources(pid)
    assert set(src) == {(10, "SPEAKER_00"), (11, "SPEAKER_01")}


def test_sources_skip_pending_and_voiceprint_answers(db):
    """Pending i avtomaticheskie podtverzhdeniya ne dayut etalony (iza obratnoy svyazi)."""
    pid = _add_profile("Petrov P.P.")
    _add_review(10, "SPEAKER_00", pid)
    _add_review(11, "SPEAKER_01", pid, status="pending", answered_by=None)
    _add_review(12, "SPEAKER_02", pid, answered_by="voiceprint")
    from mnema.core.speaker_backfill import profile_backfill_sources
    src = profile_backfill_sources(pid)
    assert src == [(10, "SPEAKER_00")]


def test_sources_dedupe_and_max_jobs(db):
    pid = _add_profile("Sidorov S.S.")
    for j in (1, 2, 3):
        _add_review(j, "SPEAKER_00", pid)
    _add_review(1, "SPEAKER_00", pid)  # duplika
    from mnema.core.speaker_backfill import profile_backfill_sources
    src = profile_backfill_sources(pid, max_jobs=2)
    assert len(src) == 2
    assert len(set(src)) == 2


# ---- backfill_profile_embeddings ----------------------------------------------
def test_backfill_without_backend_raises(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: None)
    from mnema.core.speaker_backfill import backfill_profile_embeddings
    with pytest.raises(RuntimeError):
        backfill_profile_embeddings()


def test_backfill_sets_mean_embedding(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker_backfill.cluster_embedding_for",
                        lambda job_id, cluster: [1.0, 0.0] if job_id == 10 else [0.0, 1.0])
    pid = _add_profile("Kuznetsov K.K.")
    _add_review(10, "SPEAKER_00", pid)
    _add_review(11, "SPEAKER_00", pid)
    from mnema.core.speaker_backfill import backfill_profile_embeddings
    res = backfill_profile_embeddings()
    assert "iz 2 klasterov" in res[pid]
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = s.get(VoiceProfile, pid)
        assert v.embedding is not None
        assert v.match_stats.get("backfill_clusters") == 2


def test_backfill_skips_profiles_with_existing_embedding(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker_backfill.cluster_embedding_for",
                        lambda job_id, cluster: [1.0, 0.0])
    pid = _add_profile("Already Emb.", embedding=[0.0, 0.0, 1.0])
    _add_review(10, "SPEAKER_00", pid)
    from mnema.core.speaker_backfill import backfill_profile_embeddings
    res = backfill_profile_embeddings()
    assert "skip" in res[pid]
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        assert s.get(VoiceProfile, pid).embedding == [0.0, 0.0, 1.0]


def test_backfill_dry_run_does_not_write(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker_backfill.cluster_embedding_for",
                        lambda job_id, cluster: [1.0, 0.0])
    pid = _add_profile("Dry Run.)")
    _add_review(10, "SPEAKER_00", pid)
    from mnema.core.speaker_backfill import backfill_profile_embeddings
    res = backfill_profile_embeddings(dry_run=True)
    assert "dry-run" in res[pid]
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        assert s.get(VoiceProfile, pid).embedding is None


def test_backfill_no_sources_leaves_embedding_none(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    pid = _add_profile("Unknown Nobody")
    from mnema.core.speaker_backfill import backfill_profile_embeddings
    res = backfill_profile_embeddings()
    assert "net podtverzhdennykh" in res[pid]


# ---- _run_speaker: verdict=auto zakryvaet review-avtomatom --------------------
def _mk_segments(job_id, n, cluster="SPEAKER_00"):
    from mnema.core.db import get_session
    from mnema.core.models import Job, Segment
    with get_session() as s:
        s.add(Job(id=job_id, topic="t", status="processing", source="upload"))
        for i in range(n):
            s.add(Segment(job_id=job_id, t_start=i * 5.0, t_end=i * 5.0 + 4.0,
                          text="x", speaker_id=cluster, diar_label=cluster))
        s.commit()


def _speaker_env(monkeypatch, verdict, profile_id, sim):
    import mnema.core.speaker as sp
    monkeypatch.setattr("mnema.worker.tasks._cluster_embedding",
                        lambda wav, segs, turns=None: [1.0, 0.0])
    monkeypatch.setattr(sp, "match_cluster",
                        lambda emb: (verdict, s_profile(profile_id), sim))
    monkeypatch.setattr(sp, "thresholds", lambda: (0.65, 0.45))


def s_profile(pid):
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        return s.get(VoiceProfile, pid)


def test_run_speaker_auto_closes_review(db, monkeypatch):
    pid = _add_profile("Avtomat A.A.")
    _mk_segments(900, 2)
    _speaker_env(monkeypatch, "auto", pid, 0.9)
    from mnema.worker.tasks import _run_speaker
    _run_speaker(900)
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment
    with get_session() as s:
        rr = s.scalars(select(ReviewRequest).where(ReviewRequest.job_id == 900)).all()
        assert len(rr) == 1
        assert rr[0].status == "confirmed"
        assert rr[0].answered_by == "voiceprint"
        assert rr[0].similarity == pytest.approx(0.9)
        assert rr[0].candidate_profile_id == pid
        segs = s.scalars(select(Segment).where(Segment.job_id == 900)).all()
        assert all(x.speaker_name == "Avtomat A.A." and not x.needs_review for x in segs)


def test_run_speaker_hint_leaves_review_pending(db, monkeypatch):
    pid = _add_profile("Hint H.H.")
    _mk_segments(901, 2)
    _speaker_env(monkeypatch, "hint", pid, 0.5)
    from mnema.worker.tasks import _run_speaker
    _run_speaker(901)
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest
    with get_session() as s:
        rr = s.scalars(select(ReviewRequest).where(ReviewRequest.job_id == 901)).all()
        assert len(rr) == 1 and rr[0].status == "pending"
        assert rr[0].candidate_profile_id == pid


def test_run_speaker_unresolved_pending_without_candidate(db, monkeypatch):
    _mk_segments(902, 2)
    _speaker_env(monkeypatch, "unresolved", 0, 0.1)
    from mnema.worker.tasks import _run_speaker
    _run_speaker(902)
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest
    with get_session() as s:
        rr = s.scalars(select(ReviewRequest).where(ReviewRequest.job_id == 902)).all()
        assert len(rr) == 1 and rr[0].status == "pending"
        assert rr[0].candidate_profile_id is None


def test_run_speaker_rematch_closes_previously_pending(db, monkeypatch):
    """Povtornoy matsching posle backfill: pending review s novym auto zakryvaetsya."""
    pid = _add_profile("Rematch R.R.")
    _mk_segments(903, 2)
    _add_review(903, "SPEAKER_00", None, status="pending", answered_by=None)
    _speaker_env(monkeypatch, "auto", pid, 0.8)
    from mnema.worker.tasks import _run_speaker
    _run_speaker(903)
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest
    with get_session() as s:
        rr = s.scalars(select(ReviewRequest).where(ReviewRequest.job_id == 903)).all()
        assert len(rr) == 1 and rr[0].status == "confirmed"
        assert rr[0].answered_by == "voiceprint"


def test_run_speaker_does_not_touch_human_answered(db, monkeypatch):
    """Ruchnoy otvet operatora (confirmed web) povtornyy matsching ne poremont."""
    pid_real = _add_profile("Chelovek Ch.Ch.")
    _mk_segments(904, 2)
    _add_review(904, "SPEAKER_00", pid_real, status="confirmed", answered_by="web")
    other = _add_profile("Drugoy D.D.")
    _speaker_env(monkeypatch, "auto", other, 0.91)
    from mnema.worker.tasks import _run_speaker
    _run_speaker(904)
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest
    with get_session() as s:
        rr = s.scalars(select(ReviewRequest).where(ReviewRequest.job_id == 904)).all()
        assert len(rr) == 1 and rr[0].candidate_profile_id == pid_real


def _session():
    from mnema.core.db import get_session
    return get_session()


def test_cluster_embedding_for_rejects_stub_backend(db, monkeypatch):
    """V api-konteynere bez pyannote profil' ne poluchaet 32-D hash-musor."""
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: None)
    from mnema.core.speaker import _cluster_embedding_for
    with _session() as s:
        assert _cluster_embedding_for(s, 999, "SPEAKER_00") is None


def test_cluster_embedding_for_none_when_wav_missing(db, monkeypatch):
    import pathlib
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.worker.tasks._job_paths",
                        lambda job_id: {"wav": pathlib.Path("/nonexistent.wav")})
    from mnema.core.speaker import _cluster_embedding_for
    with _session() as s:
        assert _cluster_embedding_for(s, 999, "SPEAKER_00") is None  # net wav


def test_run_speaker_rematch_refreshes_pending_hint(db, monkeypatch):
    """Povtornyy matsching (posle backfill) perenosit v pending review naydennogo
    candidate i similarity, no ne zakryvaet ego."""
    pid = _add_profile("Noviy N.N.")
    _mk_segments(905, 2)
    _add_review(905, "SPEAKER_00", None, status="pending", answered_by=None)
    _speaker_env(monkeypatch, "hint", pid, 0.5)
    from mnema.worker.tasks import _run_speaker
    _run_speaker(905)
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest
    with get_session() as s:
        rr = s.scalars(select(ReviewRequest).where(ReviewRequest.job_id == 905)).all()
        assert len(rr) == 1
        assert rr[0].status == "pending"
        assert rr[0].candidate_profile_id == pid
        assert rr[0].similarity == pytest.approx(0.5)


# ---- run_voice_enroll: avtomaticheskiy enroll posle otveta operatora ----------
def test_enroll_skips_without_backend(db, monkeypatch):
    """V srede bez pyannote (api-konteyner) hash-musor v profil' ne pishetsya."""
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: None)
    _mk_segments(910, 2)
    pid = _add_profile("Enroll Skip")
    _add_review(910, "SPEAKER_00", pid)
    from mnema.worker.tasks import run_voice_enroll
    run_voice_enroll(910, "SPEAKER_00")
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        assert s.get(VoiceProfile, pid).embedding is None


def test_enroll_sets_embedding_from_cluster(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker_backfill.cluster_embedding_for",
                        lambda job_id, cluster: [1.0, 0.0])
    _mk_segments(911, 2)
    pid = _add_profile("Enroll New")
    _add_review(911, "SPEAKER_00", pid)
    from mnema.worker.tasks import run_voice_enroll
    run_voice_enroll(911, "SPEAKER_00")
    from mnema.core.db import get_session
    from mnema.core.models import SessionEvent, VoiceProfile
    with get_session() as s:
        v = s.get(VoiceProfile, pid)
        assert v.embedding == [1.0, 0.0]
        assert v.match_stats.get("enrollments") == 1
        evs = s.scalars(select(SessionEvent).where(
            SessionEvent.job_id == 911,
            SessionEvent.event == "voice_enroll")).all()
        assert len(evs) == 1


def test_enroll_appends_ref_instead_of_blending(db, monkeypatch):
    """Ref-set vmesto EMA: novyy etapton — otdel'nyy ref; srednee embedding
    ne perepisyvaet predydushchie vklady kak odin smesannyy vektor, a max-
    cosine matsching (profile_similarity) sravnivaet so vsemi etalonami."""
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker_backfill.cluster_embedding_for",
                        lambda job_id, cluster: [0.0, 1.0])
    _mk_segments(912, 2)
    pid = _add_profile("Enroll Ref", embedding=[1.0, 0.0],
                       match_stats={"ref_sources": ["legacy"]})
    _add_review(912, "SPEAKER_00", pid)
    from mnema.worker.tasks import run_voice_enroll
    run_voice_enroll(912, "SPEAKER_00")
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    from mnema.core.speaker import cosine, profile_similarity
    with get_session() as s:
        v = s.get(VoiceProfile, pid)
        assert len(v.ref_embeddings) == 2
        assert v.ref_embeddings[1] == [0.0, 1.0]
        # maximum po refam sohranyaet oba golosa, a ne sredneye, semyayushchee v 0.707
        assert profile_similarity([1.0, 0.0], v) == pytest.approx(1.0)
        assert profile_similarity([0.0, 1.0], v) == pytest.approx(1.0)
        assert cosine([1.0, 0.0], v.embedding) < 1.0  # srednee — tol'ko kesh


def test_enroll_skips_duplicate_cluster_source(db, monkeypatch):
    """Povtornyy enroll togo zhe klastera ne mnozhit odin i tot zhe etalon."""
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker_backfill.cluster_embedding_for",
                        lambda job_id, cluster: [1.0, 0.0])
    _mk_segments(916, 2)
    pid = _add_profile("Enroll Dup")
    _add_review(916, "SPEAKER_00", pid)
    from mnema.worker.tasks import run_voice_enroll
    run_voice_enroll(916, "SPEAKER_00")
    run_voice_enroll(916, "SPEAKER_00")
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        v = s.get(VoiceProfile, pid)
        assert len(v.ref_embeddings or []) == 1
        assert v.match_stats.get("enroll_skipped_dup") == 1


def test_enroll_skips_voiceprint_auto_answer(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker_backfill.cluster_embedding_for",
                        lambda job_id, cluster: [1.0, 0.0])
    _mk_segments(913, 2)
    pid = _add_profile("Enroll Auto")
    _add_review(913, "SPEAKER_00", pid, answered_by="voiceprint")
    from mnema.worker.tasks import run_voice_enroll
    run_voice_enroll(913, "SPEAKER_00")
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        assert s.get(VoiceProfile, pid).embedding is None


def test_enroll_skips_pending_review(db, monkeypatch):
    monkeypatch.setattr("mnema.core.speaker._embedding_backend", lambda: "pyannote")
    monkeypatch.setattr("mnema.core.speaker_backfill.cluster_embedding_for",
                        lambda job_id, cluster: [1.0, 0.0])
    _mk_segments(914, 2)
    pid = _add_profile("Enroll Pending")
    _add_review(914, "SPEAKER_00", pid, status="pending", answered_by=None)
    from mnema.worker.tasks import run_voice_enroll
    run_voice_enroll(914, "SPEAKER_00")
    from mnema.core.db import get_session
    from mnema.core.models import VoiceProfile
    with get_session() as s:
        assert s.get(VoiceProfile, pid).embedding is None


def test_api_answer_review_enqueues_voice_enroll(db, tmp_path, monkeypatch):
    """Otvet POST /api/reviews/{rid} stavit v ochered speaker zadachu enroll."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr("mnema.core.queue.enqueue_job",
                        lambda name, func, *a, **kw: calls.append((name, func, a))
                        or None)
    _mk_segments(915, 2)
    rid = _add_review(915, "SPEAKER_00", None, status="pending", answered_by=None)
    from fastapi.testclient import TestClient
    from mnema.api.main import app
    client = TestClient(app)
    resp = client.post(f"/api/reviews/{rid}",
                       json={"employee_name": "Ochered O.O.", "create_new": True})
    assert resp.status_code == 200
    enr = [c for c in calls if "run_voice_enroll" in str(c[1])]
    assert len(enr) == 1
    assert enr[0][0] == "speaker"
    assert enr[0][2] == (915, "SPEAKER_00")
