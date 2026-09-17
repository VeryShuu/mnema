"""VAD (FT-13): Silero esli ustanovlen; fallback — RMS porog po PCM.

Vozvraschaet segmety rechi [{t_start, t_end}] s granicami po pauze;
korotkie (<0.5 s) otbrossyvayutsya; dlina cel 3-15 s (razrez po maksimumu).
"""
from __future__ import annotations

import wave
from pathlib import Path

TARGET_MAX_S = 15.0
MIN_SPEECH_S = 0.5


def segment_speech(wav: Path) -> list[dict]:
    backend = _get_silero()
    if backend is not None and wav.exists():
        try:
            return _silero_segments(backend, wav)
        except Exception:  # noqa: BLE001
            pass
    return _energy_segments(wav)


_silero = None


def _get_silero():
    global _silero
    if _silero is not None:
        return _silero
    try:
        import torch  # noqa: F401
        from silero_vad import load_silero_vad
        _silero = load_silero_vad()
        return _silero
    except Exception:  # noqa: BLE001
        _silero = False
        return None


def _silero_segments(model, wav: Path) -> list[dict]:
    import torch
    from silero_vad import read_wave, get_speech_timestamps
    w, sr = read_wave(str(wav))
    ts = get_speech_timestamps(w, model, sampling_rate=sr)
    return _postprocess([{"t_start": t["start"] / sr, "t_end": t["end"] / sr} for t in ts])


def _energy_segments(wav: Path) -> list[dict]:
    """Porogovoy RMS VAD bez dependency: 30 ms shkady, porog = median*3 + epsilon."""
    try:
        with wave.open(str(wav), "rb") as w:
            sr = w.getframerate()
            n = w.getnframes()
            raw = w.readframes(n)
    except Exception:  # noqa: BLE001
        return []
    struct_mod = __import__("struct")
    frame = max(1, int(sr * 0.03))
    energies = []
    total = len(raw) // 2
    samples_count = total
    for i in range(0, samples_count, frame):
        chunk = raw[i * 2:(i + frame) * 2]
        n = len(chunk) // 2
        if n == 0:
            break
        samples = struct_mod.unpack(f"<{n}h", chunk[: 2 * n])
        if not samples:
            break
        energies.append((sum(s * s for s in samples) / n) ** 0.5)
    if not energies:
        return []
    import statistics
    med = statistics.median(energies) or 1
    threshold = max(med * 3, 200)
    voice = [e > threshold for e in energies]
    segs: list[dict] = []
    start = None
    for i, v in enumerate(voice):
        t = i * 0.03
        if v and start is None:
            start = t
        elif not v and start is not None:
            if t - start >= MIN_SPEECH_S / 2:
                segs.append({"t_start": start, "t_end": t})
            start = None
    if start is not None:
        segs.append({"t_start": start, "t_end": samples_count * 0.03 if energies else start})
    return _merge_and_split(_postprocess(segs), sr)


def _merge_and_split(segs: list[dict], sr: int) -> list[dict]:
    """Skleyka segmetov s malyh zazoram (<0.3 s) i razrez slishkom dlinnyh."""
    out: list[dict] = []
    for s in segs:
        if out and s["t_start"] - out[-1]["t_end"] < 0.3:
            out[-1]["t_end"] = s["t_end"]
        else:
            out.append(dict(s))
    final: list[dict] = []
    for s in out:
        cur = s["t_start"]
        while s["t_end"] - cur > TARGET_MAX_S:
            final.append({"t_start": cur, "t_end": cur + TARGET_MAX_S})
            cur += TARGET_MAX_S
        if s["t_end"] - cur >= MIN_SPEECH_S:
            final.append({"t_start": cur, "t_end": s["t_end"]})
    return final


def _postprocess(segs: list[dict]) -> list[dict]:
    return [s for s in segs if s["t_end"] - s["t_start"] >= MIN_SPEECH_S]


def extract_wav_slice(src: Path, t0: float, t1: float, dst: Path) -> None:
    """Vyrezaet fragment WAV (dlя emgeddingov) cherez ffmpeg (tochnye shkaly)."""
    from .audio import run
    dur = max(0.1, t1 - t0)
    r = run(["ffmpeg", "-y", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}",
             "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)])
    if r.returncode != 0:
        # fallback: pryamaya nakhotka cherez wave
        with wave.open(str(src), "rb") as w:
            sr = w.getframerate()
            w.setpos(int(t0 * sr))
            frames = w.readframes(int(dur * sr))
        with wave.open(str(dst), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(frames)
