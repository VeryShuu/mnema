"""Testy remontu povrezhdennoy zapisi (regressiya job 17).

Klyuchevyevoy pattern: MediaRecorder-bez-cues (Streamed) webm — ffprobe daet
N/A/0, no polnyy dekod schityvaet ves' payload. mkvmerge takaogo «chinit» do
72 s iz 3980 s (34 MB -> 973 KB), poetomu repair sravnivaet varianty po polnomu
dekomu. Testam nuzhny ffmpeg/ffprobe; mkvmerge opcionalen.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="nuzhen ffmpeg/ffprobe")


def _streamed_webm(path: Path, seconds: int = 6) -> Path:
    """webm bez cues/duration (kak u Chrome MediaRecorder): pipe-vyvod,
    probe daet N/A, dekoderschityvaet do kontsa."""
    data = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "libopus", "-b:a", "64k", "-ar", "48000", "-f", "webm",
         "pipe:1"], capture_output=True)
    data.check_returncode()
    path.write_bytes(data.stdout)
    return path


def _cluster_offsets(data: bytes) -> list[int]:
    return [m.start() for m in re.finditer(re.escape(b"\x1f\x43\xb6\x75"), data)]


def _probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_format", str(path)], capture_output=True, text=True)
    import json
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except (ValueError, KeyError):
        return 0.0


@needs_ffmpeg
def test_decoded_duration_chitaet_stream_bez_probe(tmp_path):
    """Streamed webm: probe=0, no decoded_duration_s daet real'nyu dlitel'nost'."""
    from mnema.core.audio import decoded_duration_s
    p = tmp_path / "s.webm"
    _streamed_webm(p, 6)
    assert _probe_duration(p) == 0.0        # konteyner bez Duration
    d = decoded_duration_s(p)
    assert 4.5 <= d <= 7.5                  # dekod vsyo vidit


@needs_ffmpeg
def test_decoded_duration_garbage_i_otsutstviye_dayut_nol(tmp_path):
    from mnema.core.audio import decoded_duration_s
    bad = tmp_path / "bad.webm"
    bad.write_bytes(b"garbage" * 200)
    assert decoded_duration_s(bad) == 0.0
    assert decoded_duration_s(tmp_path / "net.webm") == 0.0


@needs_ffmpeg
def test_repair_ne_syedaet_payload_streama(tmp_path):
    """Regessiya job 17: repair ne dolzhen s"edat' payload. Podogrev:
    streamedwebm (probe=0) — posle repair probe rabotaet i dlitel'nost'
    sohranena, a ne skorochen do obryva."""
    from mnema.core.audio import repair_webm, decoded_duration_s
    p = tmp_path / "raw.webm"
    _streamed_webm(p, 6)
    src_dec = decoded_duration_s(p)
    assert repair_webm(p) is True
    assert decoded_duration_s(p) >= src_dec - 0.5
    # posle remonta probe obyazhen rabotat' (chisty konteyner s indexom)
    assert _probe_duration(p) >= src_dec - 1.0


@needs_ffmpeg
def test_repair_garbage_ne_lonit_fayl(tmp_path):
    """Nechitaemyy fayl: repair vozvraschaet False, syroye na meste, null-
    plitki (fffix/fixed) ne rashvatsya."""
    from mnema.core.audio import repair_webm
    p = tmp_path / "raw.webm"
    p.write_bytes(os.urandom(4096))
    repair_webm(p)
    assert p.exists()
    leftovers = [f.name for f in tmp_path.iterdir() if f != p]
    assert leftovers == []


@needs_ffmpeg
def test_repaired_pick_regessiya_mkvmerge_kanibal():
    """Unit: syroye 3980 s protiv mkvmerge-«pochinnennogo» 72 s — vygryvaet
    syroye; fix v predelakh slack vygoraet bonusom, deficit > slack — net."""
    from pathlib import Path as P
    from mnema.core.audio import repaired_pick
    assert repaired_pick([(P("raw"), 3980.0, False), (P("mkv"), 72.0, True)])[0] == P("raw")
    assert repaired_pick([(P("raw"), 100.0, False), (P("fix"), 99.9, True)])[0] == P("fix")
    assert repaired_pick([(P("raw"), 200.0, False), (P("fix"), 139.9, True)])[0] == P("raw")
    assert repaired_pick([(P("raw"), 0.0, False), (P("fix"), 72.0, True)])[0] == P("fix")
    assert repaired_pick([(P("a"), 0.0, False), (P("b"), 0.0, True)]) is None
    assert repaired_pick([]) is None


@needs_ffmpeg
def test_repair_obryv_posle_poloviny_sochnyaet_dekodiruyemoe(tmp_path):
    """Obryvannyy streamed-webm: polnyy dekod s'edaet do obryva; repair sohrannyaet
    eto dekodiryemoe (ne sokaishet do odnogo klastera)."""
    from mnema.core.audio import repair_webm, decoded_duration_s
    full = tmp_path / "full.webm"
    _streamed_webm(full, 6)
    data = full.read_bytes()
    offs = _cluster_offsets(data)
    assert len(offs) >= 4
    cut = tmp_path / "cut.webm"
    cut.write_bytes(data[:offs[len(offs) // 2]])
    cut_dec = decoded_duration_s(cut)
    assert cut_dec > 0  # dekod do obryva rabotaet
    assert repair_webm(cut) is True
    assert decoded_duration_s(cut) >= cut_dec - 0.5
