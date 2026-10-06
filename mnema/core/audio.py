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


AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus", ".webm", ".amr"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".flv", ".ts", ".mpg", ".mpeg", ".3gp"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


def media_ext_ok(filename: str) -> bool:
    return Path(filename).suffix.lower() in MEDIA_EXTENSIONS


def probe_media(path: Path) -> dict:
    """ffprobe: dlitelnost' i nalichie audio-dorozhki v lyubom konteyenerye.

    Vozvraschaet {"ok": bool, "duration_s": float, "error": str|None};
    ok=False esli fayl neprochitaem ili audio-stream otsutstvuet."""
    if shutil.which("ffprobe") is None:
        return {"ok": False, "duration_s": 0.0, "error": "ffprobe отсутствует в системе"}
    try:
        r = run(["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)])
    except FileNotFoundError:
        return {"ok": False, "duration_s": 0.0, "error": "ffprobe не запустился"}
    if r.returncode != 0:
        return {"ok": False, "duration_s": 0.0,
                "error": "файл повреждён или формат не поддерживается"}
    try:
        data = json.loads(r.stdout)
    except (ValueError, TypeError):
        return {"ok": False, "duration_s": 0.0, "error": "ffprobe вернул некорректный вывод"}
    streams = data.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        return {"ok": False, "duration_s": 0.0, "error": "в файле нет аудиодорожки"}
    dur = 0.0
    try:
        dur = float(data.get("format", {}).get("duration", 0) or 0)
    except (ValueError, TypeError):
        dur = 0.0
    if dur <= 0:
        for a in audio:
            try:
                dur = float(a.get("duration", 0) or 0)
                if dur > 0:
                    break
            except (ValueError, TypeError):
                continue
    if dur <= 0:
        return {"ok": False, "duration_s": 0.0, "error": "не удалось определить длительность"}
    return {"ok": True, "duration_s": dur, "error": None}


def media_to_wav16k(src: Path, wav: Path) -> None:
    """Pervaya audio-dorozhka iz video/audio -> PCM WAV 16 kHz 16-bit mono."""
    r = run(["ffmpeg", "-y", "-i", str(src), "-vn", "-map", "0:a:0",
             "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(wav)])
    if r.returncode != 0 or not wav.exists() or wav.stat().st_size <= 44:
        wav.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg media->wav: {r.stderr[-500:]}")


def media_to_mp3(src: Path, mp3: Path, bitrate: str = "64k") -> bool:
    """Pervaya audio-dorozhka -> mp3 44.1 kHz mono (vygruzka/pleer, kak u zapisey)."""
    r = run(["ffmpeg", "-y", "-i", str(src), "-vn", "-map", "0:a:0",
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


def decode_pcm16_16k(path: Path) -> bytes | None:
    """Lyuboy audioformat -> PCM s16le mono 16 kГц baytami (None esli ne chinaetsya)."""
    if shutil.which("ffmpeg") is None:
        return None
    try:
        r = subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(path),
                            "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
                           capture_output=True)
    except FileNotFoundError:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout


def snr_db(path: Path) -> float:
    """Ocenka SNR v dB: p90 (rotevoy uroven') / p10 (noys-floor) po 30-msok kadram.

    -1.0 esli fayl ne chinaetsya. Shum-ot-noisy zapis' daet ~0-2 dB;
    chistaya rech' — 10+ dB."""
    data = decode_pcm16_16k(path)
    if not data or len(data) < 4:
        return -1.0
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data[: 2 * n])
    sr = 16000
    frame = int(sr * 0.03)
    energies = []
    for i in range(0, len(samples) - frame + 1, frame):
        chunk = samples[i:i + frame]
        energies.append((sum(s * s for s in chunk) / frame) ** 0.5)
    if not energies:
        return -1.0
    srt = sorted(energies)
    noise = srt[max(0, int(len(srt) * 0.10))]
    loud = srt[min(len(srt) - 1, int(len(srt) * 0.90))]
    if noise <= 0:
        return 60.0 if loud > 0 else -1.0
    import math
    return 20.0 * math.log10(max(loud, 1e-6) / noise)


def speech_level(path: Path) -> float:
    """RMS vseh 'gromkih' kadrov (p90): mrtvy mikrofon / tishina — pochti 0."""
    data = decode_pcm16_16k(path)
    if not data or len(data) < 4:
        return 0.0
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data[: 2 * n])
    sr = 16000
    frame = int(sr * 0.03)
    energies = []
    for i in range(0, len(samples) - frame + 1, frame):
        chunk = samples[i:i + frame]
        energies.append((sum(s * s for s in chunk) / frame) ** 0.5)
    if not energies:
        return 0.0
    srt = sorted(energies)
    return srt[min(len(srt) - 1, int(len(srt) * 0.90))]
