"""Testy fallback-VAD i vetok konveyera (asr/speaker parallel'nost', mappin'g).

Regression: median*3 na dlinnoy zapisi s plotnoy rech'yu daval 0 segmentov
(28,5 "rechevyh" sekund iz 4110) — porog dolzhen schitatsya ot noise floor.
"""
import math
import random
import struct
import wave
from pathlib import Path


def _mkwav(path: Path, parts: list[tuple[float, int]], sr: int = 16000) -> None:
    """parts: [(dlit_s, RMS-amplituda)] — ton s zadannoy amplitudoy."""
    rng = random.Random(42)
    frames = []
    for dur, amp in parts:
        n = int(dur * sr)
        for i in range(n):
            tone = int(amp * math.sin(2 * math.pi * 220 * i / sr))
            noise = rng.randint(-amp // 8, amp // 8) if amp else 0
            frames.append(max(-32768, min(32767, tone + noise)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack(f"<{len(frames)}h", *frames))


def _voice_then_silence(path: Path, n_voice: int, n_sil: int,
                        amp: int = 4000, sr: int = 16000) -> None:
    """Cheredovanie: `n_voice` s rechi (ton+shum), `n_sil` tishiny; 12 ciklov."""
    _mkwav(path, [(n_voice, amp), (n_sil, 0)] * 12)


def test_energy_thresholds_splits_noise_and_voice():
    from mnema.core.vad import energy_thresholds
    energies = [5.0] * 40 + [4000.0] * 60  # 40% shum, 60% golos
    hi, lo = energy_thresholds(energies)
    assert lo < hi <= 4000.0  # golos vyse oboih poryogov
    assert hi <= 2000.0       # a shum — nizhe high


def test_energy_vad_finds_long_speech_dense_recording():
    """Regression median*3: plavnaya rech' 70% vremeni dolzhna naytis' vsya."""
    from mnema.core.vad import _energy_segments
    segs = _energy_segments(Path(__file__).parent / "fixtures" / "dense_speech.wav")
    total = sum(s["t_end"] - s["t_start"] for s in segs)
    assert segs, "VAD ne nashel nichego na zapisi s plotnoy rech'yu"
    assert total > 60, f"naydeno tol'ko {total:.1f} s rechi (ozhidalsya ~70 s)"


def test_energy_vad_splits_at_pauses_and_caps_length():
    from mnema.core.vad import TARGET_MAX_S, _energy_segments
    segs = _energy_segments(Path(__file__).parent / "fixtures" / "dense_speech.wav")
    assert all(s["t_end"] - s["t_start"] <= TARGET_MAX_S + 0.1 for s in segs)
    assert all(s["t_end"] - s["t_start"] >= 0.5 for s in segs)


def test_energy_vad_all_silence_returns_empty():
    from mnema.core.vad import _energy_segments

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "silence.wav"
        _mkwav(p, [(5.0, 0), (5.0, 2)])  # absolutnaya tishina + edimichny shum
        assert _energy_segments(p) == []


# ---- mappin'g ASR -> VAD ------------------------------------------------------

def test_map_asr_merges_overlaps_without_dupes():
    """Odin ASR-segment ne dolzhen popadat' v neskol'ko VAD (dubl' v stenogamme)."""
    from mnema.core.asr_client import _map_verbose_to_segments
    raw = [{"start": 10.0, "end": 20.0, "text": " dlinnaa replika ",
            "avg_logprob": -0.1}]
    vad = [{"t_start": 9.0, "t_end": 14.0},   # peresechenie 4 s -> hozyan
           {"t_start": 15.0, "t_end": 16.0}]  # peresechenie 1 s -> pustaya
    out = _map_verbose_to_segments({"segments": raw, "language": "ru"}, vad)
    assert out[0]["text"] == "dlinnaa replika"
    assert out[1]["text"] == ""  # ne dubl'


def test_map_asr_concatenates_several_asr_per_vad():
    from mnema.core.asr_client import _map_verbose_to_segments
    raw = [{"start": 1.0, "end": 4.0, "text": "pervaya", "avg_logprob": -0.2},
           {"start": 5.0, "end": 8.0, "text": "vtoraya", "avg_logprob": -0.4},
           {"start": 9.5, "end": 11.0, "text": "trestaya", "avg_logprob": -0.6}]
    vad = [{"t_start": 0.5, "t_end": 12.0}]
    out = _map_verbose_to_segments({"segments": raw, "language": "ru"}, vad)
    assert out[0]["text"] == "pervaya vtoraya trestaya"
    assert 0.2 < out[0]["confidence"] < 0.7  # srednee exp po logprob


# ---- vetki konveyera ----------------------------------------------------------

import os

import pytest
from sqlalchemy import create_engine, select, StaticPool

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    from mnema.core.db import Base, set_engine_for_test
    import mnema.core.models  # noqa: F401
    Base.metadata.create_all(engine)
    set_engine_for_test(engine)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("DIARIZER", "off")
    monkeypatch.setenv("VAD", "energy")
    monkeypatch.setenv("ASR_BASE_URL", "")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("HF_TOKEN", "")
    # forced direct mode: na mashine mozhet byd' chuzhoy redis v zone
    # dostupnosti (port iz REDIS_URL) — testy ne dolzny tam nichego enqueueit'.
    import mnema.core.queue as q
    monkeypatch.setattr(q, "_queues", {}, raising=False)
    monkeypatch.setattr(q, "get_queue", lambda name: (None, "direct"))
    # zaglushka ASR: vsegda pusty text (vsetak pishetsya v kolonku vetki)
    import mnema.core.asr_client as ac
    monkeypatch.setattr(
        ac, "transcribe_segments",
        lambda wav, vad: [{"text": "slova", "lang": "ru", "confidence": 0.9}
                          for _ in vad])
    return tmp_path


def _mkjob_with_segs(job_id: int = 1):
    import mnema.core.models as m
    from mnema.core.db import get_session
    with get_session() as s:
        s.add(m.Job(id=job_id, topic="T", status="processing",
                    source="upload"))
        for i in range(3):
            s.add(m.Segment(job_id=job_id, t_start=i * 5.0,
                            t_end=i * 5.0 + 3.0, text="",
                            speaker_source="unresolved", raw_track="web-mix"))
        s.commit()


def _wav_for(job_id: int, tmp_path: Path) -> Path:
    d = tmp_path / "jobs" / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    wav = d / "meeting.wav"
    if not wav.exists():
        _voice_then_silence(wav, 4, 2)
    return wav


def test_speaker_branch_then_asr_branch_merge_columns(env):
    """Vetki pisayut v raznye kolonki: order ne vazhen, dannye ne tryatsya."""
    _mkjob_with_segs()
    _wav_for(1, Path(os.environ["DATA_DIR"]))
    from mnema.core.db import get_session
    from mnema.core.models import Segment
    from mnema.worker.tasks import run_asr_text, run_speaker_id

    run_speaker_id(1)   # speaker-pervoy
    run_asr_text(1)     # text-vtoroy
    with get_session() as s:
        segs = s.scalars(select(Segment).where(Segment.job_id == 1)).all()
        assert segs
        for seg in segs:
            assert seg.text == "slova"
            assert seg.speaker_id == "SPEAKER_UNKNOWN"


def test_asr_branch_rerun_idempotent(env):
    _mkjob_with_segs()
    _wav_for(1, Path(os.environ["DATA_DIR"]))
    from mnema.core.db import get_session
    from mnema.core.models import Job, Segment
    from mnema.worker.tasks import run_asr_text
    run_asr_text(1)
    run_asr_text(1)     # HT-2: povtor ne lomayet dannye
    with get_session() as s:
        segs = s.scalars(select(Segment).where(Segment.job_id == 1)).all()
        assert len(segs) == 3
        assert all(seg.text == "slova" for seg in segs)
        assert s.get(Job, 1).status == "processing"


def test_orchestrate_direct_mode_end_to_end(env):
    """Bez redis: prep -> asr -> speaker -> join posledovatel'no."""
    from mnema.core.db import get_session
    from mnema.core.models import Job
    from mnema.core.pipeline import transition
    from mnema.worker.tasks import run_orchestrate
    _mkjob_with_segs()
    _wav_for(1, Path(os.environ["DATA_DIR"]))
    run_orchestrate(1)
    with get_session() as s:
        j = s.get(Job, 1)
        assert j.status in ("awaiting_llm", "awaiting_review", "done")
