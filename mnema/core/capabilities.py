"""Sostoyanie capabilities (FT-39): computed + leshchevy health-probe s cooldown.

not_configured: URL pust; configured: URL zadan;
misconfigured: URL zadan, no probe ne otvechaet.
probe: GET /models (llm), GET /models ili /health (asr) — deshevo.
"""
from __future__ import annotations

import time

import httpx

from .registry import state_for

_cooldowns: dict[str, tuple[float, dict]] = {}
PROBE_COOLDOWN_S = 30


def _probe(base_url: str, api_key: str | None, probe_path: str = "/models") -> tuple[bool, str]:
    url = base_url.rstrip("/") + probe_path
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = httpx.get(url, headers=headers, timeout=5.0)
        if r.status_code < 500:
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def capability_status(force: bool = False) -> dict:
    out: dict[str, dict] = {}
    for cap in ("llm", "asr"):
        base = state_for(cap)
        import os
        if cap == "llm":
            url = os.environ.get("LLM_BASE_URL", "").strip()
            key = os.environ.get("LLM_API_KEY", "").strip()
        else:
            url = os.environ.get("ASR_BASE_URL", "").strip()
            key = os.environ.get("ASR_API_KEY", "").strip()
        if base["state"] == "configured" and url:
            now = time.monotonic()
            last = _cooldowns.get(cap)
            if force or last is None or (now - last[0]) > PROBE_COOLDOWN_S:
                ok, detail = _probe(url, key)
                base["probe"] = {"ok": ok, "detail": detail}
                base["state"] = "configured" if ok else "misconfigured"
                _cooldowns[cap] = (now, base)
            else:
                base["probe"] = last[1].get("probe")
                base["state"] = last[1]["state"]
        out[cap] = base
    return out
