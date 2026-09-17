"""Diarizaciya (FT-25): pyannote 3.1 esli ustanovlena; fallback — odin 'unknown'.

pyannote davlaet segmety s SPEAKER_XX. Bez ney (dev/CPU) vse segmety poluchayut
odin klubter 'SPEAKER_UNKNOWN' — sistema prodolzhaet rabotat' (kholodny start),
no utochneniya idut v review. Peremennaya DIARIZER=off vypykaet pyannote.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")


def diarize(wav: Path, vad_segments: list[dict]) -> list[str]:
    backend = _get_pyannote()
    if backend is None:
        return ["SPEAKER_UNKNOWN"] * len(vad_segments)
    try:
        return _pyannote_labels(backend, wav, vad_segments)
    except Exception:  # noqa: BLE001
        return ["SPEAKER_UNKNOWN"] * len(vad_segments)


_pipe = None


def _get_pyannote():
    global _pipe
    if _pipe is not None:
        return _pipe
    if os.environ.get("DIARIZER", "").lower() in ("off", "none", "energy"):
        _pipe = False
        return None
    try:
        from pyannote.audio import Pipeline
        import torch
        tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
        pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1",
                                        use_auth_token=tok)
        if pipe is None:
            _pipe = False
            return None
        if os.environ.get("ASR_DEVICE", "cpu").startswith("cuda") and torch.cuda.is_available():
            pipe.to(torch.device("cuda"))
        _pipe = pipe
        return _pipe
    except Exception:  # noqa: BLE001
        _pipe = False
        return None


def _pyannote_labels(pipe, wav: Path, vad_segments: list[dict]) -> list[str]:
    diar = pipe(str(wav))
    out: list[str] = []
    for seg in vad_segments:
        t = (seg["t_start"] + seg["t_end"]) / 2
        label = _label_at(diar, t) or "SPEAKER_UNKNOWN"
        out.append(label)
    return out


def _label_at(diar, t: float) -> str | None:
    best = None
    best_dur = 0.0
    for turn, _, spk in diar.itertracks(yield_label=True):
        if turn.start <= t <= turn.end:
            d = turn.end - turn.start
            if d > best_dur:
                best, best_dur = spk, d
    return best
