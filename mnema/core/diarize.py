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

import logging
import os
import time
import warnings
from pathlib import Path

log = logging.getLogger("mnema.diarize")

warnings.filterwarnings("ignore")


def _retry_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("DIAR_RETRY_SEC", "600")))
    except ValueError:
        return 600.0


def diarize(wav: Path, vad_segments: list[dict]) -> list[str]:
    labels, _turns, _ratios = diarize_with_turns(wav, vad_segments)
    return labels


def diarize_with_turns(wav: Path, vad_segments: list[dict]
                       ) -> tuple[list[str], list | None, list | None]:
    """(labels, turns, cov_ratios); turns — [[t_start, t_end, label], ...] po
    vsey zapisi (None esli diari niedostupen), cov_ratios — dlya kazhdogo
    VAD-segmenta dolya dominantnogo govoryashchego. Metka VAD-segmenta — po
    pokrytiyu (dominantny), a ne po seredine: segment na styke smeny golosa
    popadaet k kryashchemu ego golosu, no low-coverage segment yemnitsya kak
    overlap."""
    backend = _get_pyannote()
    if backend is None:
        return ["SPEAKER_UNKNOWN"] * len(vad_segments), None, None
    try:
        res = _pyannote_labels(backend, wav, vad_segments)
        if isinstance(res, tuple):
            return res
        return res, None, None  # testovye zaglushki, vozvrashchayushchie tol'ko metki
    except Exception as e:  # noqa: BLE001
        log.warning("pyannote run failed (%r) — %s segments as SPEAKER_UNKNOWN",
                    e, len(vad_segments))
        return ["SPEAKER_UNKNOWN"] * len(vad_segments), None, None


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
            log.info("pyannote disabled via DIARIZER — all segments SPEAKER_UNKNOWN")
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
            log.warning("pyannote: HF_TOKEN not set — loading gated model "
                        "pyannote/speaker-diarization-3.1 may fail")
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
            log.info("pyannote: device=%s fp16=%s batch=%s",
                     device, _fp16_enabled(), batch)
        _pipe = pipe
        return _pipe
    except Exception as e:  # noqa: BLE001
        log.warning("pyannote pipeline load failed (%r) — retrying in %.0fs; "
                    "segments are SPEAKER_UNKNOWN until then",
                    e, _retry_sec())
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
            log.warning("fp16 diarization failed (%s); falling back to fp32", e)
    return pipe(str(wav))


def _pyannote_labels(pipe, wav: Path,
                     vad_segments: list[dict]) -> tuple[list[str], list, list]:
    diar = _run_pipeline(pipe, wav)
    turns = [[float(t.start), float(t.end), str(spk)]
             for t, _, spk in diar.itertracks(yield_label=True)]
    out: list[str] = []
    ratios: list[float] = []
    for seg in vad_segments:
        label, ratio = _dominant_label(turns, seg["t_start"], seg["t_end"])
        out.append(label or "SPEAKER_UNKNOWN")
        ratios.append(ratio)
    return out, turns, ratios


def coverage_by_label(turns: list, t0: float, t1: float) -> dict[str, float]:
    """Dolya (s) pokrytiya kazhdogo govoryashchego na otrezke [t0, t1]."""
    cov: dict[str, float] = {}
    for ts, te, spk in turns:
        ov = min(te, t1) - max(ts, t0)
        if ov > 0:
            cov[spk] = cov.get(spk, 0.0) + ov
    return cov


def _dominant_label(turns: list, t0: float, t1: float) -> tuple[str | None, float]:
    """(govoryashchiy s naibol'shim pokrytiem, dolya pokrytiya segmenta).

    Ranshe bralsya povorot, soderzhashchiy seredinyu, — segment na styke
    dvukh govorivykh (a VAD-rezhet i po 15 s prinuditel'no) otdestovalsya
    odnomu golosu tselikom i portil etalon klastera. Dolya < 1 signaliruet
    na lozhenie (overlap) — takiy fragment v embedding brat nel'zya."""
    dur = max(1e-9, t1 - t0)
    cov = coverage_by_label(turns, t0, t1)
    if not cov:
        return None, 0.0
    best = max(cov, key=cov.get)
    return best, min(1.0, cov[best] / dur)


def label_at_turns(turns: list, t0: float, t1: float) -> tuple[str | None, float]:
    """Pokrytie dlya vneshnikh vyzov (seriailizovannye turns iz BDD)."""
    return _dominant_label(turns, t0, t1)
