"""Testy chankovaniya gigastt-uploadov (HTTP 400 invalid_multipart na >~48 MB)."""
import io
import wave

import pytest


def _make_wav(path, seconds, rate=16000, nch=1, sw=2):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * (rate * seconds * nch))


@pytest.fixture
def fake_post(monkeypatch):
    calls = []

    def fake(base, headers, model, body):
        with wave.open(io.BytesIO(body), "rb") as w:
            dur = w.getnframes() / w.getframerate()
        calls.append({"size": len(body), "dur": dur})
        seg = {"start": max(0.0, dur - 1.0), "end": dur, "text": "tst"}
        return {"segments": [seg], "language": "ru"}

    monkeypatch.setattr("mnema.core.asr_client._gigastt_post", fake)
    return calls


def test_small_file_single_request(tmp_path, monkeypatch, fake_post):
    from mnema.core.asr_client import _gigastt_chunks
    monkeypatch.setenv("ASR_MAX_UPLOAD_MB", "1")
    wav = tmp_path / "a.wav"
    _make_wav(wav, 10)  # 10 s = 320 KB < 1 MB
    out = _gigastt_chunks(wav)
    assert len(fake_post) == 1
    assert out["language"] == "ru"


def test_big_file_chunked_and_shifted(tmp_path, monkeypatch, fake_post):
    from mnema.core.asr_client import _gigastt_chunks
    monkeypatch.setenv("ASR_MAX_UPLOAD_MB", "1")
    wav = tmp_path / "b.wav"
    _make_wav(wav, 60)  # 60 s = 1.92 MB -> minimum 2 chanka
    out = _gigastt_chunks(wav)
    assert len(fake_post) >= 2
    assert all(c["size"] <= 1024 * 1024 + 1024 for c in fake_post)
    ends = [s["end"] for s in out["segments"]]
    assert max(ends) > 55  # segment konca filya sohranil absolyutnoe vremya


def test_large_limit_single_request_no_chunking(tmp_path, monkeypatch, fake_post):
    """gigastt 2.21 (GIGASTT_BODY_LIMIT_BYTES=1 GiB): fail 48 MB bar'er,
    pri ASR_MAX_UPLOAD_MB>=razmeru filya-upload idet odnim zaprosom."""
    from mnema.core.asr_client import _gigastt_chunks
    monkeypatch.setenv("ASR_MAX_UPLOAD_MB", "1024")
    wav = tmp_path / "big.wav"
    _make_wav(wav, 30 * 60)  # 30 min = 57.6 MB > stalevshie 48 MB
    out = _gigastt_chunks(wav)
    assert len(fake_post) == 1
    assert fake_post[0]["size"] > 48 * 1024 * 1024
    assert out["language"] == "ru"


def test_dedupe_segments_overlap():
    from mnema.core.asr_client import _dedupe_segments
    raw = [{"start": 0.0, "end": 4.0, "text": "a"},
           {"start": 4.9, "end": 5.5, "text": "granica"},
           {"start": 4.95, "end": 5.52, "text": "granica"},
           {"start": 10.0, "end": 12.0, "text": "b"}]
    out = _dedupe_segments(raw)
    assert [s["text"] for s in out] == ["a", "granica", "b"]


def test_dedupe_keeps_distinct_segments():
    from mnema.core.asr_client import _dedupe_segments
    raw = [{"start": 0.0, "end": 5.0, "text": "a"},
           {"start": 5.5, "end": 9.0, "text": "b"}]
    assert len(_dedupe_segments(raw)) == 2


def test_max_upload_env_default_and_override(monkeypatch):
    from mnema.core.asr_client import _max_upload_bytes
    monkeypatch.delenv("ASR_MAX_UPLOAD_MB", raising=False)
    assert _max_upload_bytes() == 40 * 1024 * 1024
    monkeypatch.setenv("ASR_MAX_UPLOAD_MB", "10")
    assert _max_upload_bytes() == 10 * 1024 * 1024
    monkeypatch.setenv("ASR_MAX_UPLOAD_MB", "abc")
    assert _max_upload_bytes() == 40 * 1024 * 1024


def test_empty_wav_raises(tmp_path):
    from mnema.core.asr_client import _gigastt_chunks
    wav = tmp_path / "e.wav"
    _make_wav(wav, 0)
    with pytest.raises(RuntimeError):
        _gigastt_chunks(wav)
