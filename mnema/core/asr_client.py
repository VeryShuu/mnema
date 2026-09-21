"""ASR-klient (FT-12/FT-40). Vneshniy gigastt cherez OpenAI-sovmestimy endpoint.

GET /v1/audio/transcriptions (multipart WAV) -> verbose_json segment.
Faster-whisper fallback esli ASR=not_configured/misconfigured (FT-39).
"""
from __future__ import annotations

import os
from pathlib import Path

from .capabilities import capability_status


def engine_name() -> str:
    return os.environ.get("ASR_ENGINE", "gigastt").strip() or "gigastt"


def transcribe_segments(wav: Path, vad_segments: list[dict]) -> list[dict]:
    """Vozvraschaet [{text, lang, confidence}] paralleno vad_segments."""
    caps = capability_status()["asr"]
    use_remote = caps["state"] == "configured"
    if use_remote:
        try:
            return _gigastt_transcribe(wav, vad_segments)
        except Exception as e:  # noqa: BLE001
            print(f"ASR remote fail ({e}); fallback faster-whisper", flush=True)
    return _faster_whisper_transcribe(wav, vad_segments)


def _gigastt_transcribe(wav: Path, vad_segments: list[dict]) -> list[dict]:
    import httpx
    base = os.environ.get("ASR_BASE_URL", "").rstrip("/")
    key = os.environ.get("ASR_API_KEY", "").strip()
    model = os.environ.get("ASR_MODEL", "ml_ctc_large").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    with open(wav, "rb") as f:
        files = {"file": (wav.name, f, "audio/wav")}
        data = {"model": model, "language": "auto", "response_format": "verbose_json"}
        r = httpx.post(f"{base}/v1/audio/transcriptions", headers=headers,
                       files=files, data=data, timeout=600.0)
    if r.status_code >= 400:
        raise RuntimeError(f"gigastt HTTP {r.status_code}: {r.text[:200]}")
    payload = r.json()
    return _map_verbose_to_segments(payload, vad_segments)


def _map_verbose_to_segments(payload: dict, vad_segments: list[dict]) -> list[dict]:
    """Sootnesti segmenty ASR po vremeni k VAD-segmentam (perekrysti po maksimumu)."""
    raw = payload.get("segments") or []
    lang = payload.get("language")
    out: list[dict] = []
    for seg in vad_segments:
        best = _best_overlap(raw, seg["t_start"], seg["t_end"])
        if best is not None:
            out.append({"text": best.get("text", "").strip(),
                        "lang": best.get("lang", lang),
                        "confidence": best.get("avg_logprob") and _conf_from_logprob(best["avg_logprob"])})
        else:
            out.append({"text": "", "lang": lang, "confidence": None})
    return out


def _best_overlap(raw: list[dict], t0: float, t1: float) -> dict | None:
    best = None
    best_ov = 0.0
    for r in raw:
        rs = r.get("start", 0)
        re = r.get("end", 0)
        ov = max(0, min(t1, re) - max(t0, rs))
        if ov > best_ov:
            best, best_ov = r, ov
    return best or (raw[0] if len(raw) == 1 else None)


def _conf_from_logprob(v: float) -> float:
    try:
        import math
        return max(0.0, min(1.0, math.exp(v)))
    except Exception:  # noqa: BLE001
        return None


_whisper = None


def _whisper_compute_type(device: str) -> str:
    """float16 na GPU, int8 na CPU; OVERRIDE: WHISPER_COMPUTE_TYPE."""
    override = os.environ.get("WHISPER_COMPUTE_TYPE", "").strip()
    if override:
        return override
    return "float16" if device.startswith("cuda") else "int8"


def _faster_whisper_transcribe(wav: Path, vad_segments: list[dict]) -> list[dict]:
    global _whisper
    model_id = os.environ.get("ASR_MODEL", "large-v3")
    if model_id in ("ml_ctc_large", "ml_ctc"):
        model_id = os.environ.get("WHISPER_FALLBACK_MODEL", "large-v3")
    if _whisper is None:
        try:
            from faster_whisper import WhisperModel
            device = os.environ.get("ASR_DEVICE", "cpu")
            _whisper = WhisperModel(model_id, device=device,
                                    compute_type=_whisper_compute_type(device))
        except Exception as e:  # noqa: BLE001
            print(f"faster-whisper niedostupen ({e}) — pustaya stenogramma", flush=True)
            return [{"text": "", "lang": None, "confidence": None} for _ in vad_segments]
    try:
        seg_iter, _info = _whisper.transcribe(str(wav), vad_filter=False)
        raw = [{"start": s.start, "end": s.end, "text": s.text,
                "avg_logprob": getattr(s, "avg_logprob", None)} for s in seg_iter]
    except Exception as e:  # noqa: BLE001
        print(f"faster-whisper transcibe fail: {e}", flush=True)
        raw = []
    return _map_verbose_to_segments({"segments": raw, "language": "ru"}, vad_segments)
