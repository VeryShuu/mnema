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


def test_retrofit_moves_punctuation_into_segments():
    """gigastt verbose_text: segments[].text syrye, punktuacia/registr v top-level text."""
    from mnema.core.asr_client import _retrofit_punctuation
    payload = {
        "text": "Привет, как дела? Потом продолжим.",
        "segments": [
            {"start": 0.0, "end": 2.0, "text": " привет как"},
            {"start": 2.5, "end": 4.0, "text": " дела"},
            {"start": 5.0, "end": 7.0, "text": " потом продолжим"},
        ],
    }
    out = _retrofit_punctuation(payload)
    assert [s["text"].strip() for s in out["segments"]] == [
        "Привет, как", "дела?", "Потом продолжим."]


def test_retrofit_no_top_text_keeps_raw():
    from mnema.core.asr_client import _retrofit_punctuation
    payload = {"segments": [{"start": 0.0, "end": 1.0, "text": " teskt"}]}
    out = _retrofit_punctuation(payload)
    assert out["segments"][0]["text"] == " teskt"


def test_retrofit_poor_match_keeps_raw():
    """Esli top-level text sovsem ne pro eto (sboy vravnivaniya) — ne portim segmenty."""
    from mnema.core.asr_client import _retrofit_punctuation
    payload = {
        "text": "sovershenno drugoy tekst sovsem",
        "segments": [{"start": 0.0, "end": 1.0, "text": " a b c d e f g h"}],
    }
    out = _retrofit_punctuation(payload)
    assert out["segments"][0]["text"] == " a b c d e f g h"


def test_retrofit_partial_itn_word_insertion():
    """ITN menyaet slova (ciframi): nesovpavshie syrye slova ostayutsya,
    punktuaciya perenositsya tam, gde slovo sovpadalo."""
    from mnema.core.asr_client import _retrofit_punctuation
    payload = {
        "text": "24, хорошо.",
        "segments": [
            {"start": 0.0, "end": 2.0, "text": " двадцать четыре"},
            {"start": 2.5, "end": 4.0, "text": " хорошо"},
        ],
    }
    out = _retrofit_punctuation(payload)
    assert out["segments"][0]["text"].strip() == "двадцать четыре,"
    assert out["segments"][1]["text"].strip() == "хорошо."


def test_retrofit_empty_segment_keeps_empty_text():
    from mnema.core.asr_client import _retrofit_punctuation
    payload = {
        "text": "Odin dva.",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "odin"},
            {"start": 1.0, "end": 1.0, "text": ""},
            {"start": 1.0, "end": 2.0, "text": "dva"},
        ],
    }
    out = _retrofit_punctuation(payload)
    assert out["segments"][1]["text"] == ""


def test_gigastt_transcribe_applies_retrofit(tmp_path, monkeypatch):
    """Regressiya: stenogramma poluchaet punktuaciu iz top-level text gigastt."""
    from mnema.core import asr_client

    def fake_post(base, headers, model, body):
        return {"text": "Zaprotokoliruem vstrechu.",
                "segments": [{"start": 0.0, "end": 2.0, "text": " zaprotokoliruem vstrechu"}],
                "language": "ru"}

    monkeypatch.setattr(asr_client, "_gigastt_post", fake_post)
    wav = tmp_path / "x.wav"
    _make_wav(wav, 5)
    out = asr_client._gigastt_transcribe(wav, [{"t_start": 0.0, "t_end": 5.0}])
    assert out[0]["text"] == "Zaprotokoliruem vstrechu."


def test_chunked_path_retrofits_each_chunk(tmp_path, monkeypatch):
    """Punktuaciya ne teryaetsya pri chunkovanii bolshogo fajla."""
    from mnema.core import asr_client

    def fake_post(base, headers, model, body):
        return {"text": "Predlozhenie.",
                "segments": [{"start": 0.0, "end": 2.0, "text": " predlozhenie"}],
                "language": "ru"}

    monkeypatch.setattr(asr_client, "_gigastt_post", fake_post)
    monkeypatch.setenv("ASR_MAX_UPLOAD_MB", "1")
    wav = tmp_path / "c.wav"
    _make_wav(wav, 60)
    out = asr_client._gigastt_chunks(wav)
    assert all(s["text"].strip() == "Predlozhenie." for s in out["segments"])
