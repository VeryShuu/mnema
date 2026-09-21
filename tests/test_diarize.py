"""Diari/embieddingi pyannote (FT-25/FT-27) na sinteze dva-golosa.

Propuskayutsya (skip), nie tie HF_TOKEN/gated-riesursy: v CI bez HF-dostupa
diariizatsiya ne dolna lamatsya. Proverka zhivogo razdeleniya: dva spikera ->
>= 2 klastera; embedding odnogo golosa stabilen, raznyh golosov - razlichaetsya.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HF_TOKEN"),
    reason="nuzhen HF_TOKEN dlya pyannote gated wies")


@pytest.fixture(autouse=True)
def _reset_caches():
    """Ne dadim global'nomu kechu pyannote tech' cherez granicy testov."""
    from mnema.core import diarize as dz
    from mnema.core import speaker as sp
    dz._pipe = None
    sp._backend_cache.clear()
    sp._inference_cache.clear()
    yield
    dz._pipe = None
    sp._backend_cache.clear()
    sp._inference_cache.clear()


def _espeak(path: Path, pitch: int, speed: int, text: str):
    if shutil.which("espeak-ng") is None:
        pytest.skip("espeak-ng niedostupen")
    subprocess.run(["espeak-ng", "-v", "ru", "-p", str(pitch), "-s", str(speed),
                    "-w", str(path), text], check=True, capture_output=True)
    if not path.exists():
        pytest.skip("espeak-ng nie zapisal wav")


@pytest.fixture(scope="module")
def voices(tmp_path_factory):
    d = tmp_path_factory.mktemp("voices")
    a, b = d / "spk_a.wav", d / "spk_b.wav"
    _espeak(a, 30, 130, "Privet segodnya my obsuzhdaem plan rabot po proektu")
    _espeak(b, 90, 115, "A ya hotchu dobavit zamechaniya po srokam sdachi")
    two = d / "two.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(a), "-i", str(b),
         "-filter_complex",
         "[0]adelay=0|0[x];[1]adelay=4000|4000[y];[x][y]amix=inputs=2:duration=longest,volume=2",
         str(two)], check=True, capture_output=True)
    return a, b, two


def test_diarizer_loads_pipeline(voices):
    from mnema.core import diarize as dz
    dz._pipe = None
    import atexit
    atexit.register(lambda: setattr(dz, "_pipe", None))
    pipe = dz._get_pyannote()
    assert pipe is not None, "Pipeline.from_pretrained вернул None (net wies/token)"


def test_diarize_separates_two_speakers(voices):
    from mnema.core import diarize as dz
    dz._pipe = None
    _, _, two = voices
    segs = [{"t_start": t, "t_end": t + 1.5}
            for t in [0.3, 1.5, 2.7, 4.3, 5.5, 7.0, 8.5]]
    labels = dz.diarize(two, segs)
    assert len(labels) == len(segs)
    assert len(set(labels)) >= 2, f"vse v odnom klastere: {labels}"
    first_half = {l for s, l in zip(segs, labels) if s["t_start"] < 4}
    second_half = {l for s, l in zip(segs, labels) if s["t_start"] >= 4.3}
    assert first_half and second_half and first_half != second_half


def test_diarize_off_env_forces_unknown(monkeypatch, voices):
    from mnema.core import diarize as dz
    monkeypatch.setenv("DIARIZER", "off")
    monkeypatch.setattr(dz, "_pipe", None)
    _, _, two = voices
    segs = [{"t_start": 0.3, "t_end": 1.8}, {"t_start": 4.3, "t_end": 5.8}]
    assert dz.diarize(two, segs) == ["SPEAKER_UNKNOWN", "SPEAKER_UNKNOWN"]


def test_embedding_backend_is_pyannote(voices):
    from mnema.core.speaker import _backend_cache, _embedding_backend
    _backend_cache.clear()
    assert _embedding_backend() == "pyannote"


def test_embedding_dim_is_512(voices):
    from mnema.core.speaker import compute_embedding_for_file
    emb = compute_embedding_for_file(voices[0])
    assert len(emb) == 512, f"ozhidalsya 512-D voiceprint, polucheno {len(emb)}"


def test_embedding_same_speaker_stable(voices):
    from mnema.core.speaker import compute_embedding_for_file, cosine
    a, b, _ = voices
    assert cosine(compute_embedding_for_file(a),
                  compute_embedding_for_file(a)) > 0.999


def test_embedding_different_speakers_farther_apart(voices):
    from mnema.core.speaker import compute_embedding_for_file, cosine
    a, b, _ = voices
    ea1 = compute_embedding_for_file(a)
    ea2 = compute_embedding_for_file(a)
    eb = compute_embedding_for_file(b)
    assert cosine(ea1, eb) < cosine(ea1, ea2)
