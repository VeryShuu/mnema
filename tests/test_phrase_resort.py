"""Фразовая voiceprint-перепроверка (_phrase_resort) + DIAR_SEG_STEP/EMBED_MIN_S.

Pyannote v etih testah podmenyaetsya: extract_wav_slice pishet v fail klyuch
[ t0, t1 ], kompyuter embeddingov vozvraschaet vektor po klyucu — emedding
frazy opredelyayet 'skripovannyu' pravdu o tom, kto govoril.
"""
import datetime as dt
import os

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive-phrase")
os.environ.setdefault("DATA_DIR", "/tmp/test-data-phrase")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-phrase.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # zavedomo niet
os.environ["LLM_BASE_URL"] = ""
os.environ["ASR_BASE_URL"] = ""

import pytest
from sqlalchemy import create_engine, select


@pytest.fixture()
def db(monkeypatch, tmp_path):
    # FAJOVAYA (a ne :memory:/StaticPool): match_cluster otkryvayet vlozhennuyu
    # sessiyu — na odnom obshem soedinenii StaticPool on otkatyvanet vneshniy
    # tranzaktsiya; failovaya BDD zdes' kak v produkcii.
    import mnema.core.models  # noqa: F401 — registracija tablic v metadata do create_all
    engine = create_engine(f"sqlite:///{tmp_path}/mnema-test.db",
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    yield engine


def _mk_job(job_id, segs_spec):
    """segs_spec: [(t_start, t_end, parts|None, label)] -> segment id list."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, Segment
    with get_session() as s:
        s.add(Job(id=job_id, topic="t", status="processing", source="upload"))
        ids = []
        for t0, t1, parts, label in segs_spec:
            seg = Segment(job_id=job_id, t_start=t0, t_end=t1,
                          speaker_id=label, diar_label=label,
                          text=" ".join(p["t"] for p in (parts or [])),
                          asr_json=parts)
            s.add(seg)
            s.flush()
            ids.append(seg.id)
        s.commit()
        return ids


def _script_embeddings(monkeypatch, table):
    """table: {(s, e): vector|None} dlya fraz; klyuch — imenno timeshampy chasti."""
    from pathlib import Path

    def fake_extract(src, t0, t1, dst):
        Path(dst).write_text(f"{t0:.3f};{t1:.3f}", encoding="utf-8")

    def fake_compute(path):
        t0s, t1s = Path(path).read_text(encoding="utf-8").split(";")
        return table.get((round(float(t0s), 3), round(float(t1s), 3)))

    monkeypatch.setattr("mnema.core.vad.extract_wav_slice", fake_extract)
    monkeypatch.setattr("mnema.core.speaker.compute_embedding_for_file",
                        fake_compute)


def _resort(job_id, centroids, locked=()):
    from pathlib import Path
    from mnema.core.db import get_session
    from mnema.worker.tasks import _phrase_resort
    with get_session() as s:
        from mnema.core.models import Segment
        segs = list(s.scalars(select(Segment).where(
            Segment.job_id == job_id).order_by(Segment.t_start)).all())
        n = _phrase_resort(s, job_id, Path("/unused.wav"), segs,
                           dict(centroids), set(locked))
        s.commit()
        return n


def _all_segs(job_id):
    from mnema.core.db import get_session
    from mnema.core.models import Segment
    with get_session() as s:
        return list(s.scalars(select(Segment).where(
            Segment.job_id == job_id).order_by(Segment.t_start)).all())


# ---- perenos frazy v sinteticheskiy podklaster ------------------------------

def test_intruder_phrase_moves_to_synthetic_cluster(db, monkeypatch):
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    _mk_job(100, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "moya fraza"},
                              {"s": 4.0, "e": 6.0, "t": "chuuzaya"}],
                 "SPEAKER_00")])
    n = _resort(100, {"SPEAKER_00": [1.0, 0.0]})
    assert n == 1
    segs = _all_segs(100)
    assert len(segs) == 2
    assert segs[0].text == "moya fraza"
    assert segs[0].speaker_id == "SPEAKER_00"
    assert segs[1].speaker_id == "SPEAKER_00-p%s" % segs[0].id
    assert segs[1].text == "chuuzaya"
    assert (segs[1].t_start, segs[1].t_end) == (4.0, 6.0)
    assert segs[1].needs_review is True
    assert segs[1].speaker_name is None


def test_synthetic_cluster_gets_pending_review(db, monkeypatch):
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    _mk_job(101, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                              {"s": 4.0, "e": 6.0, "t": "b"}], "SPEAKER_00")])
    _resort(101, {"SPEAKER_00": [1.0, 0.0]})
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment
    with get_session() as s:
        new_label = s.scalars(select(Segment.speaker_id).where(
            Segment.job_id == 101,
            Segment.speaker_id.like("SPEAKER_00-p%"))).first()
        rv = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == 101,
            ReviewRequest.cluster_label == new_label)).first()
        assert rv is not None and rv.status == "pending"


# ---- perenos v uzvestnyy klaster ---------------------------------------------

def test_phrase_matching_other_cluster_label(db, monkeypatch):
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    _mk_job(102, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                              {"s": 4.0, "e": 6.0, "t": "b"}], "SPEAKER_00")])
    _resort(102, {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]})
    segs = _all_segs(102)
    assert len(segs) == 2
    assert segs[1].speaker_id == "SPEAKER_01"


def test_existing_other_cluster_review_not_duplicated(db, monkeypatch):
    """U uzvestnogo klastera review iz main-cikla yest' — vtoroy ne sodat'.
    Klyuchi vektorov takovy, chto vtoraya fraza nahodit SPEAKER_01, a ne
    sintetiku."""
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    _mk_job(103,
            [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                         {"s": 4.0, "e": 6.0, "t": "b"}], "SPEAKER_00")])
    _resort(103, {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]})
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest
    with get_session() as s:
        rvs = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == 103,
            ReviewRequest.cluster_label == "SPEAKER_01")).all()
        assert len(rvs) == 0  # izvestnye klastery review ne poluchayut zdes'


# ---- yakor' (pervaya gruppa) menyaet klaster segmenta -------------------------

def test_anchor_change_relabels_segment(db, monkeypatch):
    """Fraza chuzhogo golosa v NACHALE stroke: sam segment perehodit v drugoy
    klaster, svoy frazy idut otdel'nym segmentom."""
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    _mk_job(104, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "chuzhaya"},
                              {"s": 4.0, "e": 6.0, "t": "moya"}], "SPEAKER_00")])
    _resort(104, {"SPEAKER_00": [0.0, 1.0], "SPEAKER_01": [1.0, 0.0]})
    segs = _all_segs(104)
    assert len(segs) == 2
    assert segs[0].speaker_id == "SPEAKER_01"
    assert segs[0].text == "chuzhaya"
    assert segs[1].speaker_id == "SPEAKER_00"
    assert segs[1].text == "moya"


# ---- sliyanie grupp i propuskayemye sluchai ----------------------------------

def test_consecutive_same_label_parts_merge_into_one_piece(db, monkeypatch):
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 5.0): [1.0, 0.0],
                                     (6.0, 8.0): [0.0, 1.0],
                                     (9.0, 11.0): [0.0, 1.0]})
    _mk_job(105, [(0.0, 11.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                               {"s": 4.0, "e": 5.0, "t": "b"},
                               {"s": 6.0, "e": 8.0, "t": "c"},
                               {"s": 9.0, "e": 11.0, "t": "d"}],
                   "SPEAKER_00")])
    n = _resort(105, {"SPEAKER_00": [1.0, 0.0]})
    assert n == 1
    segs = _all_segs(105)
    assert [s.text for s in segs] == ["a b", "c d"]


def test_single_label_line_untouched(db, monkeypatch):
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.9, 0.1]})
    _mk_job(106, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                              {"s": 4.0, "e": 6.0, "t": "b"}], "SPEAKER_00")])
    assert _resort(106, {"SPEAKER_00": [1.0, 0.0]}) == 0
    assert len(_all_segs(106)) == 1


def test_short_phrase_not_verified(db, monkeypatch):
    """Fraza koriche PHRASE_VERIFY_MIN_S ne proveryaetsya i ostaetsya baze."""
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0]})
    _mk_job(107, [(0.0, 15.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                               {"s": 14.5, "e": 15.0, "t": "b"}],
                  "SPEAKER_00")])
    assert _resort(107, {"SPEAKER_00": [1.0, 0.0]}) == 0


def test_keep_threshold(db, monkeypatch):
    """Cos 0.5 s bazoy (>= PHRASE_KEEP_COS 0.3) — fraza ostaetsya, nesmotrya
    na bolee vysokoe shodstvo s chuzhim centrodom (net otryva po pravilu)."""
    other = [0.0, 1.0]
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.6, 0.8]})
    _mk_job(108, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                              {"s": 4.0, "e": 6.0, "t": "b"}], "SPEAKER_00")])
    assert _resort(108, {"SPEAKER_00": [1.0, 0.0],
                         "SPEAKER_01": other}) == 0


def test_locked_cluster_untouched(db, monkeypatch):
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    _mk_job(109, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                              {"s": 4.0, "e": 6.0, "t": "b"}], "SPEAKER_00")])
    assert _resort(109, {"SPEAKER_00": [1.0, 0.0]}, locked={"SPEAKER_00"}) == 0
    assert len(_all_segs(109)) == 1


def test_kill_switch_disables(db, monkeypatch):
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    monkeypatch.setenv("PHRASE_VERIFY", "0")
    _mk_job(110, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                              {"s": 4.0, "e": 6.0, "t": "b"}], "SPEAKER_00")])
    assert _resort(110, {"SPEAKER_00": [1.0, 0.0]}) == 0
    assert len(_all_segs(110)) == 1


def test_multi_voice_segment_skipped(db, monkeypatch):
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    from mnema.core.db import get_session
    ids = _mk_job(111, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                                    {"s": 4.0, "e": 6.0, "t": "b"}],
                         "SPEAKER_00")])
    with get_session() as s:
        from mnema.core.models import Segment
        s.get(Segment, ids[0]).speaker_source = "multi_voice"
        s.commit()
    assert _resort(111, {"SPEAKER_00": [1.0, 0.0]}) == 0
    assert len(_all_segs(111)) == 1


def test_names_from_resident_segments_propagate(db, monkeypatch):
    """Imya vtorogo klastera, uzhe prislvoennoe (otvet operatora/voiceprint),
    peredaetsya utochennoy fraze."""
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    _mk_job(112, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                              {"s": 4.0, "e": 6.0, "t": "b"}], "SPEAKER_00"),
                  (20.0, 25.0, None, "SPEAKER_01")])
    from mnema.core.db import get_session
    from mnema.core.models import Segment
    with get_session() as s:
        for seg in s.scalars(select(Segment).where(
                Segment.job_id == 112)).all():
            if seg.speaker_id == "SPEAKER_01":
                seg.speaker_name = "Petrov P.P."
                seg.speaker_source = "voiceprint"
        s.commit()
    _resort(112, {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]})
    segs = _all_segs(112)
    moved = [s for s in segs if s.text == "b"][0]
    assert moved.speaker_name == "Petrov P.P."
    assert moved.needs_review is False


# ---- _supersede_empty_reviews -------------------------------------------------

def _add_review(job_id, cluster, status="pending"):
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest
    with get_session() as s:
        s.add(ReviewRequest(job_id=job_id, cluster_label=cluster,
                            status=status))
        s.commit()


def _reviews(job_id):
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest
    with get_session() as s:
        return {r.cluster_label: r.status for r in s.scalars(
            select(ReviewRequest).where(ReviewRequest.job_id == job_id))}


def test_supersede_closes_empty_synthetic_cluster(db):
    _mk_job(120, [(0.0, 5.0, None, "SPEAKER_00")])
    _add_review(120, "SPEAKER_00-x123")
    from mnema.core.db import get_session
    from mnema.worker.tasks import _supersede_empty_reviews
    with get_session() as s:
        assert _supersede_empty_reviews(s, 120) == 1
        s.commit()
    assert _reviews(120)["SPEAKER_00-x123"] == "superseded"


def test_supersede_keeps_empty_plain_cluster(db):
    """Obychnuyu pyannote-metku bez fragmentov bez operatora ne zakryvaem."""
    _mk_job(121, [(0.0, 5.0, None, "SPEAKER_00")])
    _add_review(121, "SPEAKER_01")
    from mnema.core.db import get_session
    from mnema.worker.tasks import _supersede_empty_reviews
    with get_session() as s:
        assert _supersede_empty_reviews(s, 121) == 0
        s.commit()
    assert _reviews(121)["SPEAKER_01"] == "pending"


def test_supersede_ignores_cluster_with_segments(db):
    _mk_job(122, [(0.0, 5.0, None, "SPEAKER_00-x9")])
    _add_review(122, "SPEAKER_00-x9")
    from mnema.core.db import get_session
    from mnema.worker.tasks import _supersede_empty_reviews
    with get_session() as s:
        assert _supersede_empty_reviews(s, 122) == 0
        s.commit()
    assert _reviews(122)["SPEAKER_00-x9"] == "pending"


# ---- env-konfiguratsii ---------------------------------------------------------

def test_seg_step_default_zero(monkeypatch):
    from mnema.core.diarize import _seg_step
    monkeypatch.delenv("DIAR_SEG_STEP", raising=False)
    assert _seg_step() == 0.0


def test_seg_step_value_and_garbage(monkeypatch):
    from mnema.core.diarize import _seg_step
    monkeypatch.setenv("DIAR_SEG_STEP", "0.5")
    assert _seg_step() == 0.5
    monkeypatch.setenv("DIAR_SEG_STEP", "abc")
    assert _seg_step() == 0.0
    monkeypatch.setenv("DIAR_SEG_STEP", "-1")
    assert _seg_step() == 0.0


def test_embed_min_s(monkeypatch):
    from mnema.worker.tasks import _embed_min_s
    monkeypatch.delenv("EMBED_MIN_S", raising=False)
    assert _embed_min_s() == 1.5
    monkeypatch.setenv("EMBED_MIN_S", "1.0")
    assert _embed_min_s() == 1.0
    monkeypatch.setenv("EMBED_MIN_S", "x")
    assert _embed_min_s() == 1.5


def test_phrase_threshold_env(monkeypatch):
    from mnema.worker.tasks import (_phrase_keep_cos, _phrase_match_cos,
                                    _phrase_verify_min_s)
    monkeypatch.delenv("PHRASE_KEEP_COS", raising=False)
    monkeypatch.delenv("PHRASE_MATCH_COS", raising=False)
    monkeypatch.delenv("PHRASE_VERIFY_MIN_S", raising=False)
    assert (_phrase_keep_cos(), _phrase_match_cos(), _phrase_verify_min_s()) \
        == (0.30, 0.45, 1.0)
    monkeypatch.setenv("PHRASE_KEEP_COS", "0.4")
    monkeypatch.setenv("PHRASE_MATCH_COS", "0.5")
    monkeypatch.setenv("PHRASE_VERIFY_MIN_S", "2")
    assert (_phrase_keep_cos(), _phrase_match_cos(), _phrase_verify_min_s()) \
        == (0.4, 0.5, 2.0)
    monkeypatch.setenv("PHRASE_KEEP_COS", "junk")
    assert _phrase_keep_cos() == 0.30


# ---- perenos frazy v sinteticheskiy podklaster ------------------------------

def _mk_job_full(job_id, segs_spec, turns=None):
    """kak _mk_job, no s engine/confidence i diar_turns dlya proverka
    nasledovaniya i sinkhronizatsii s turns."""
    from mnema.core.db import get_session
    from mnema.core.models import Job, Segment
    with get_session() as s:
        s.add(Job(id=job_id, topic="t", status="processing", source="upload",
                  diar_turns=turns))
        ids = []
        for t0, t1, parts, label in segs_spec:
            seg = Segment(job_id=job_id, t_start=t0, t_end=t1,
                          speaker_id=label, diar_label=label,
                          text=" ".join(p["t"] for p in (parts or [])),
                          asr_json=parts, engine="test-engine", confidence=0.77)
            s.add(seg)
            s.flush()
            ids.append(seg.id)
        s.commit()
        return ids


def test_new_segments_inherit_engine_and_confidence(db, monkeypatch):
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    _mk_job_full(140, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                                   {"s": 4.0, "e": 6.0, "t": "b"}], "SPEAKER_00")])
    _resort(140, {"SPEAKER_00": [1.0, 0.0]})
    segs = _all_segs(140)
    assert len(segs) == 2
    for sx in segs:
        assert sx.engine == "test-engine"
        assert sx.confidence == 0.77


def test_resort_updates_diar_turns(db, monkeypatch):
    """Perenesennaya fraza otpechatana v turns: okno chuzhoy frazy s metkoy
    bazy zameneno; vnedi okna turns ne izmenilos (regressiya rassoglasovaniya
    s expand_segments pri rendere)."""
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 6.0): [0.0, 1.0]})
    turns = [[0.0, 6.0, "SPEAKER_00"]]
    _mk_job_full(141, [(0.0, 6.0, [{"s": 1.0, "e": 3.0, "t": "a"},
                                   {"s": 4.0, "e": 6.0, "t": "b"}], "SPEAKER_00")],
                 turns=turns)
    _resort(141, {"SPEAKER_00": [1.0, 0.0]})
    from mnema.core.db import get_session
    from mnema.core.models import Job, Segment
    with get_session() as s:
        new_label = s.scalars(select(Segment.speaker_id).where(
            Segment.job_id == 141,
            Segment.speaker_id.like("SPEAKER_00-p%"))).first()
        jt = s.get(Job, 141).diar_turns
    assert [list(t) for t in jt] == [[0.0, 4.0, "SPEAKER_00"],
                                     [4.0, 6.0, new_label]]


def test_apply_turn_edits_shape():
    from mnema.worker.tasks import _apply_turn_edits
    turns = [[0.0, 10.0, "SPEAKER_00"],
             [12.0, 20.0, "SPEAKER_01"]]
    # perenos [2,4) v B: razrez + vstavka; chuzhoy turn (SPEAKER_01) netronut;
    # perenos na chuzhoy turn (from=SPEAKER_00, vneshnee pokrytie) ne menyaet ego
    out = _apply_turn_edits(turns, [(2.0, 4.0, "B", "SPEAKER_00"),
                                    (15.0, 16.0, "B", "SPEAKER_00")])
    assert out == [[0.0, 2.0, "SPEAKER_00"], [2.0, 4.0, "B"],
                   [4.0, 10.0, "SPEAKER_00"],
                   [12.0, 20.0, "SPEAKER_01"]]


def test_apply_turn_edits_empty_and_garbage():
    from mnema.worker.tasks import _apply_turn_edits
    assert _apply_turn_edits(None, [(1, 2, "B", "A")]) == []
    assert _apply_turn_edits([[0.0, 1.0, "A"]], []) == [[0.0, 1.0, "A"]]


# ---- in-protsessny fast path extract_wav_slice -------------------------------

def test_wav_slice_inproc_fast_path(tmp_path, monkeypatch):
    """PCM16 mono 16k rezhetsya bez subprotsessa ffmpeg."""
    import struct
    import wave as wv
    from mnema.core.vad import extract_wav_slice
    src = tmp_path / "a.wav"
    with wv.open(str(src), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(struct.pack(f"<{16000 * 10}h",
                                  *[(i % 700) - 350 for i in range(16000 * 10)]))
    monkeypatch.setattr("mnema.core.audio.run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("ffmpeg vyvany na PCM16 16k mono")))
    dst = tmp_path / "b.wav"
    extract_wav_slice(src, 2.0, 4.0, dst)
    with wv.open(str(dst)) as w:
        assert w.getnframes() == 16000 * 2
        data = w.readframes(w.getnframes())
    # sootvetstvuyushchiy kusok istochnika
    with wv.open(str(src)) as w:
        w.setpos(2 * 16000)
        want = w.readframes(2 * 16000)
    assert data == want


def test_wav_slice_falls_back_for_foreign_format(tmp_path):
    """Ne 16k/mono/PCM16 — fast path otpuskanet (vozvrat False), layout prochie
    idut cherez ffmpeg-vetku: proveryaet lish' chto on nichego ne pishet lozh'yu
    i ne padayet na proveryke formata."""
    import wave as wv
    from mnema.core.vad import _wav_slice_inproc
    src = tmp_path / "a.wav"
    with wv.open(str(src), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\0" * 1000)
    assert _wav_slice_inproc(src, 0.0, 0.5, tmp_path / "b.wav") is False
    assert _wav_slice_inproc(tmp_path / "net.wav", 0.0, 0.5,
                             tmp_path / "b.wav") is False


# ---- vyzy `_phrase_resort` iz inside `_run_speaker` ------------------------

def test_run_speaker_invokes_resort(db, monkeypatch):
    from mnema.core.db import get_session
    from mnema.core.models import ReviewRequest, Segment, SessionEvent
    pid_a = None
    with get_session() as s:
        from mnema.core.models import VoiceProfile
        v = VoiceProfile(employee_name="Dokladchik D.D.",
                         consent_ts=dt.datetime.now(dt.timezone.utc))
        s.add(v)
        s.commit()
        pid_a = v.id
    _script_embeddings(monkeypatch, {(1.0, 3.0): [1.0, 0.0],
                                     (4.0, 8.0): [0.0, 1.0]})
    # dva 'klastera' iz 'diarizatsii': vtorogo net v segmensah — utochennaya
    # fraza uydet v sinteticheskiy podklaster
    ids = _mk_job(130, [(0.0, 8.0, [{"s": 1.0, "e": 3.0, "t": "moya"},
                                    {"s": 4.0, "e": 8.0, "t": "ne moya"}],
                        "SPEAKER_00")])
    import mnema.core.speaker as sp
    monkeypatch.setattr("mnema.worker.tasks._cluster_embedding",
                        lambda wav, segs, turns=None:
                        ([1.0, 0.0] if segs and segs[0].speaker_id == "SPEAKER_00"
                         else None))

    class P:
        id = pid_a
        employee_name = "Dokladchik D.D."
        match_stats = None

    monkeypatch.setattr(sp, "match_cluster",
                        lambda emb: ("auto", P(), 0.9) if emb == [1.0, 0.0]
                        else ("unresolved", None, 0.0))
    from mnema.worker.tasks import _run_speaker
    _run_speaker(130)
    segs = _all_segs(130)
    assert len(segs) == 2
    assert segs[0].speaker_name == "Dokladchik D.D."
    assert segs[1].speaker_id.startswith("SPEAKER_00-p")
    assert segs[1].needs_review is True
    with get_session() as s:
        assert s.scalars(select(SessionEvent).where(
            SessionEvent.job_id == 130,
            SessionEvent.event == "phrase_resort")).first() is not None
        rv = s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == 130,
            ReviewRequest.cluster_label == segs[1].speaker_id)).first()
        assert rv is not None and rv.status == "pending"
