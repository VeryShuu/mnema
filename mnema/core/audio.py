"""Rabota so zvukom: ffmpeg/mkvmerge (FT-6/FT-41)."""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
import wave
from pathlib import Path


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def mkvmerge_available() -> bool:
    return shutil.which("mkvmerge") is not None


def repair_webm(webm: Path) -> bool:
    """Pochinit' povrezhdennyy pri padenii webm (FT-6): mkvmerge -> .fixed.webm."""
    if not mkvmerge_available():
        return False
    out = webm.with_suffix(".fixed.webm")
    r = run(["mkvmerge", "-o", str(out), str(webm)])
    if r.returncode != 0 and not out.exists():
        return False
    # mkvmerge vsegda piшет warnings v stderr; schitaem uspehom esli fayl est'
    if out.exists() and out.stat().st_size > 0:
        shutil.move(str(out), str(webm))
        return True
    out.unlink(missing_ok=True)
    return False


def webm_to_wav16k(webm: Path, wav: Path) -> None:
    """webm (opus 48k) -> PCM WAV 16 kHz 16-bit mono (FT-6)."""
    r = run(["ffmpeg", "-y", "-err_detect", "ignore_err", "-i", str(webm),
             "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(wav)])
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg webm->wav: {r.stderr[-500:]}")


def wav16k_to_mp3(wav: Path, mp3: Path, bitrate: str = "64k") -> bool:
    """Rasprostorny mp3 dlyavygruzki (FT-41a): 44.1 kHz mono ~64 kbit/s.

    Vozvraschaet False esli mp3-encoder niedostupen (togda vydayetsya webm).
    """
    r = run(["ffmpeg", "-y", "-i", str(wav), "-ar", "44100", "-ac", "1",
             "-b:a", bitrate, str(mp3)])
    if r.returncode != 0 or not mp3.exists() or mp3.stat().st_size == 0:
        mp3.unlink(missing_ok=True)
        return False
    return True


def webm_to_mp3(webm: Path, mp3: Path, bitrate: str = "64k") -> bool:
    """Iz syrovogo webm srazu mp3 (ne zhdya ASR — FT-41a: mp3 poyavlyaetsya srazu)."""
    r = run(["ffmpeg", "-y", "-err_detect", "ignore_err", "-i", str(webm),
             "-ar", "44100", "-ac", "1", "-b:a", bitrate, str(mp3)])
    ok = r.returncode == 0 and mp3.exists() and mp3.stat().st_size > 0
    if not ok:
        mp3.unlink(missing_ok=True)
    return ok


def wav_duration_s(wav: Path) -> float:
    try:
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:  # noqa: BLE001
        return ffprobe_duration(wav)


def ffprobe_duration(path: Path) -> float:
    if shutil.which("ffprobe") is None:
        return 0.0
    try:
        r = run(["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", str(path)])
    except FileNotFoundError:
        return 0.0
    if r.returncode != 0:
        return 0.0
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def rms_at(wav: Path, t: float, dur: float = 1.0) -> float:
    """RMS signaliza v okne dlya provery tihi (FT-4)."""
    try:
        with wave.open(str(wav), "rb") as w:
            fr = w.getframerate()
            w.setpos(int(max(0, t) * fr))
            frames = w.readframes(int(dur * fr))
    except Exception:  # noqa: BLE001
        return 0.0
    if not frames:
        return 0.0
    n = len(frames) // 2
    samples = struct.unpack(f"<{n}h", frames[: 2 * n])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / n) ** 0.5
