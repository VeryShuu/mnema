"""Diarizaciya (FT-25): pyannote 3.1 esli ustanovlena; fallback — odin 'unknown'.

pyannote davlaet segmety s SPEAKER_XX. Bez ney (dev/CPU) vse segmety poluchayut
odin klubter 'SPEAKER_UNKNOWN' — sistema prodolzhaet rabotat' (kholodny start),
no utochneniya idut v review. Peremennaya DIARIZER=off vypykaet pyannote.

Sboy zagruzki pipeline bol'she ne memoiziruetsya navsegda: prichina vsegda
logirruetsya, a popytka zagruzki povtoryaetsya cherez DIAR_RETRY_SEC
(po defaulte 600 s), chtoby vremenno nedostupny HF (gated-modeli, setevoj
sboy) ne otklyuchal diariizatsiyu v processe workera do samogo restarta.
"""
from __future__ import annotations

import os
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")


def _retry_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("DIAR_RETRY_SEC", "600")))
    except ValueError:
        return 600.0


def diarize(wav: Path, vad_segments: list[dict]) -> list[str]:
    backend = _get_pyannote()
    if backend is None:
        return ["SPEAKER_UNKNOWN"] * len(vad_segments)
    try:
        return _pyannote_labels(backend, wav, vad_segments)
    except Exception as e:  # noqa: BLE001
        print(f"pyannote: vypolnenie sdalosya s oshibkoy ({e!r}) — "
              f"{len(vad_segments)} segm kak SPEAKER_UNKNOWN", flush=True)
        return ["SPEAKER_UNKNOWN"] * len(vad_segments)


# Sostoyanie _pipe:
#   None    — eshche ne pytalis' zagruzit'
#   False   — popytka sdalas'; povtor cherez DIAR_RETRY_SEC
#   "off"   — otklyucheno cherez DIARIZER (povtorov net)
#   Pipeline — uspesno zagruzhen
_pipe = None
_failed_ts = 0.0


def _get_pyannote():
    global _pipe, _failed_ts
    if os.environ.get("DIARIZER", "").lower() in ("off", "none", "energy"):
        if _pipe is not False and _pipe != "off":
            print("pyannote: otklyuchen cherez DIARIZER — vse segmy "
                  "SPEAKER_UNKNOWN", flush=True)
        _pipe = "off"
        return None
    if _pipe == "off":
        return None
    if _pipe:
        return _pipe
    if _pipe is False and time.monotonic() - _failed_ts < _retry_sec():
        return None
    try:
        from pyannote.audio import Pipeline
        import torch
        tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
        if not tok:
            print("pyannote: HF_TOKEN ne zadan — zagruzka gated-modeli "
                  "pyannote/speaker-diarization-3.1 mozhet pagibnut'",
                  flush=True)
        use_gpu = (os.environ.get("ASR_DEVICE", "cpu").startswith("cuda")
                   and torch.cuda.is_available())
        pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1",
                                        use_auth_token=tok)
        if pipe is None:
            raise RuntimeError("Pipeline.from_pretrained vernul None "
                               "(net HF_TOKEN / net access k gated-modeli)")
        # v pyannote 3.x batch-razmery ne prokhodyat cherez from_pretrained:
        # vyznachaem ih posle zagruzki (segmentation — vnutrenniy Inference,
        # embedding chitaetsya v execute() kak samostoyatel'nyy atribut).
        batch = max(1, int(os.environ.get("DIAR_BATCH_SIZE", "4")))
        if use_gpu:
            batch = max(1, int(os.environ.get("DIAR_BATCH_SIZE", "32")))
        try:
            pipe._segmentation.batch_size = batch
            pipe.embedding_batch_size = batch
        except Exception:  # noqa: BLE001
            pass
        if use_gpu:
            device = torch.device(os.environ.get("ASR_DEVICE", "cuda:0"))
            pipe.to(device)
            print(f"pyannote: device={device} fp16={_fp16_enabled()} "
                  f"batch={batch}", flush=True)
        _pipe = pipe
        return _pipe
    except Exception as e:  # noqa: BLE001
        print(f"pyannote: zagruzka pipeline sdalasya ({e!r}) — "
              f"povtor spustya {_retry_sec():.0f} s; do nego vse segmy "
              "SPEAKER_UNKNOWN", flush=True)
        _pipe = False
        _failed_ts = time.monotonic()
        return None


def _fp16_enabled() -> bool:
    return (os.environ.get("DIAR_FP16", "1") not in ("0", "off", "false")
            and os.environ.get("ASR_DEVICE", "cpu").startswith("cuda"))


def _run_pipeline(pipe, wav: Path):
    """Zapusk v fp16 cherez autocast (tensor cores): v pyannote 3.x vkhod vsegda
    float32, poetomu half() na vekhah padaet — autocast konvertit aktivacii sam."""
    import torch
    if _fp16_enabled() and torch.cuda.is_available():
        try:
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                return pipe(str(wav))
        except Exception as e:  # noqa: BLE001
            print(f"fp16 diari ne udalas' ({e}); vozvrat k fp32", flush=True)
    return pipe(str(wav))


def _pyannote_labels(pipe, wav: Path, vad_segments: list[dict]) -> list[str]:
    diar = _run_pipeline(pipe, wav)
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
