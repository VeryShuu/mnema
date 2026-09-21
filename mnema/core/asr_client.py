"""ASR-klient (FT-12/FT-40). Vneshniy gigastt cherez OpenAI-sovmestimy endpoint.

GET /v1/audio/transcriptions (multipart WAV) -> verbose_json segment.
Faster-whisper fallback esli ASR=not_configured/misconfigured (FT-39).
"""
from __future__ import annotations

import io
import os
import wave
from pathlib import Path

from .capabilities import capability_status


def engine_name() -> str:
    return os.environ.get("ASR_ENGINE", "gigastt").strip() or "gigastt"


def _max_upload_bytes() -> int:
    """LIMIT: gigastt otkluchaet telos zaprosa svershe ~48 MB (HTTP 400
    invalid_multipart). PO-UMOLCHANIYU 40 MB, OVERRIDE: ASR_MAX_UPLOAD_MB."""
    mb = os.environ.get("ASR_MAX_UPLOAD_MB", "40").strip() or "40"
    try:
        return max(1, int(float(mb))) * 1024 * 1024
    except ValueError:
        return 40 * 1024 * 1024


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
    payload = _gigastt_chunks(wav)
    return _map_verbose_to_segments(payload, vad_segments)


def _gigastt_post(base: str, headers: dict, model: str, body: bytes) -> dict:
    import httpx
    files = {"file": ("chunk.wav", io.BytesIO(body), "audio/wav")}
    data = {"model": model, "language": "auto", "response_format": "verbose_json"}
    r = httpx.post(f"{base}/v1/audio/transcriptions", headers=headers,
                   files=files, data=data, timeout=600.0)
    if r.status_code >= 400:
        raise RuntimeError(f"gigastt HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _gigastt_chunks(wav: Path) -> dict:
    """Post WAV chunkami: gigastt otkluchaet bolshie multipart-tela (400).

    Chanki wyravnenny po kadram PCM; segmenty sovmeschayutsya s smeshcheniem
    po vremeni. Esli WAV ne PCM (chitaet'sya nechedrez `wave`) — odin zabros.
    """
    base = os.environ.get("ASR_BASE_URL", "").rstrip("/")
    key = os.environ.get("ASR_API_KEY", "").strip()
    model = os.environ.get("ASR_MODEL", "ml_ctc_large").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    with wave.open(str(wav), "rb") as w:
        nch, sw, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        nframes = w.getnframes()
        if nframes <= 0:
            raise RuntimeError("pustoy WAV")
        max_bytes = _max_upload_bytes()
        chunk_frames = max(1, max_bytes // (nch * sw))
        if chunk_frames >= nframes:
            w.rewind()
            return _gigastt_post(base, headers, model, _wav_bytes(w.readframes(nframes), nch, sw, rate))

        raw: list[dict] = []
        lang = None
        step_frames = max(1, chunk_frames - rate)  # overlapping 1 s granicy
        pos = 0
        while pos < nframes:
            take = min(chunk_frames, nframes - pos)
            w.setpos(pos)
            frames = w.readframes(take)
            payload = _gigastt_post(base, headers, model, _wav_bytes(frames, nch, sw, rate))
            offset = pos / rate
            for s in payload.get("segments") or []:
                s = dict(s)
                s["start"] = s.get("start", 0) + offset
                s["end"] = s.get("end", 0) + offset
                raw.append(s)
            lang = lang or payload.get("language")
            if pos + take >= nframes:
                break
            pos = min(pos + step_frames, nframes - 1)
        return {"segments": _dedupe_segments(raw), "language": lang}


def _dedupe_segments(raw: list[dict]) -> list[dict]:
    """Ubrat dubli v zone perekrytiya chankov: pri sil'nom peresechenii
    ostaet'sya bolee dlinnyy segment."""
    out: list[dict] = []
    for s in sorted(raw, key=lambda x: (x.get("start", 0), x.get("end", 0))):
        if out:
            prev = out[-1]
            ov = min(prev.get("end", 0), s.get("end", 0)) - max(prev.get("start", 0), s.get("start", 0))
            dur_prev = max(prev.get("end", 0) - prev.get("start", 0), 1e-9)
            dur_s = max(s.get("end", 0) - s.get("start", 0), 1e-9)
            if ov > 0 and ov >= 0.85 * min(dur_prev, dur_s):
                if dur_s > dur_prev:
                    out[-1] = s
                continue
        out.append(s)
    return out


def _wav_bytes(frames: bytes, nch: int, sw: int, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


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
