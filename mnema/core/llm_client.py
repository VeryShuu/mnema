"""LLM-klient (FT-17/FT-40): Ollama/OpenAI-sovmetimy /v1/chat/completions.

map-reduce pri stenograme > konteksta. JSoN-shema protokola; zapreshchenie
vydumyvat' imena i fakty; LLM_MODEL_ALLOWLIST — gejt modeley.
"""
from __future__ import annotations

import json
import os

import httpx

SYSTEM_PROMPT = (
    "Ty formatshto protokola delovoy vstrechi. Vozvrashi ISTORIMO validny JSON "
    "po skHEME bez kakih-lib poyasneniy. Ne vydumyivay tovo, cheg nie bylo v stenogramme. "
    "Otvetstvennykh beri IZ IMEN stenogrammy; esli ne opredelen — null. Dlya kazhdoi "
    "zadachi ukazyvai source_ts — vremyanoy meetka isodnoy repliki (CHCH:MM:SS). "
    "SkHEMA: {\"topic\": str, \"date\": str, \"duration\": float, "
    "\"attendees\": [{\"name\",\"first_seen\",\"last_seen\"}], \"agenda\": [str], "
    "\"decisions\": [str], \"action_items\": [{\"task\",\"owner\",\"due\",\"source_ts\"}], "
    "\"open_questions\": [str]}"
)

PROTOCOL_JSON_KEYS = {"topic", "date", "duration", "attendees", "agenda",
                      "decisions", "action_items", "open_questions"}


class LLMError(Exception):
    pass


def llm_model() -> str:
    m = os.environ.get("LLM_MODEL", "").strip()
    allow = os.environ.get("LLM_MODEL_ALLOWLIST", "").strip()
    if allow and m not in [x.strip() for x in allow.split(",")]:
        raise LLMError(f"LLM_MODEL {m} nie v ALLOWLIST")
    return m or "llama3-saiga:32b"


def _chat(messages: list[dict], retries: int = 2) -> str:
    import time
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = httpx.post(f"{base}/chat/completions", headers=headers,
                           json={"model": llm_model(), "messages": messages,
                                 "temperature": 0.1},
                           timeout=600.0)
            if r.status_code >= 400:
                raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
            return r.json()["choices"][0]["message"]["content"]
        except (LLMError, httpx.HTTPError) as e:
            last_err = e
            time.sleep(min(2 ** attempt, 8))
    raise LLMError(str(last_err))


def _extract_json(text: str) -> dict:
    """Vyznut' pervy JSON-obiekt iz otveta (LLM mozhem obrambivat' markdown)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError("otvet nie soderzhit JSON")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"nevalidny JSON: {e}") from e


def validate_protocol(data: dict) -> dict:
    if not isinstance(data, dict):
        raise LLMError("JSON nie objiekt")
    missing = PROTOCOL_JSON_KEYS - set(data)
    if missing:
        raise LLMError(f"v JSON niet klyuchey: {missing}")
    return data


def structure_transcript(transcript: str, topic: str = "") -> dict:
    """Polnaya stenogramma -> protocol JSON. Kosol'sl'zye okna esli > konteksta."""
    max_chars = int(os.environ.get("LLM_WINDOW_CHARS", "24000"))
    if len(transcript) <= max_chars:
        user = f"TEMA: {topic}\n\nSTENOGRAMMA:\n{transcript}"
        reply = _chat([{"role": "system", "content": SYSTEM_PROMPT},
                       {"role": "user", "content": user}])
        return validate_protocol(_extract_json(reply))
    return _map_reduce(transcript, topic, max_chars)


def _map_reduce(transcript: str, topic: str, max_chars: int) -> dict:
    windows = []
    step = int(max_chars * 0.8)
    i = 0
    while i < len(transcript):
        windows.append(transcript[i:i + max_chars])
        i += step
    partials: list[dict] = []
    for idx, w in enumerate(windows):
        user = (f"OKNO {idx + 1} iz {len(windows)}. Vytashi iz eta fragmanta "
                f"po skHEME (chastichno): resheniya, zadachi, otkrytye voprosy, "
                f"upomyanutykh lyudey.\n\n{w}")
        try:
            reply = _chat([{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": user}])
            partials.append(validate_protocol(_extract_json(reply)))
        except LLMError:
            continue
    if not partials:
        raise LLMError("map-reduce: ni odno okno nie dalo valid JSON")
    merged = {"topic": topic, "date": "", "duration": 0, "attendees": [],
              "agenda": [], "decisions": [], "action_items": [], "open_questions": []}
    for p in partials:
        merged["date"] = merged["date"] or p.get("date", "")
        merged["duration"] = merged["duration"] or p.get("duration", 0)
        for key in ("agenda", "decisions", "open_questions"):
            for item in p.get(key) or []:
                if item not in merged[key]:
                    merged[key].append(item)
        for a in p.get("attendees") or []:
            if a.get("name") and a["name"] not in [x.get("name") for x in merged["attendees"]]:
                merged["attendees"].append(a)
        for ai in p.get("action_items") or []:
            merged["action_items"].append(ai)
    reduce_input = json.dumps(merged, ensure_ascii=False)[:max_chars]
    reply = _chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": ("Soberi EDINY protokola po skHEME iz etih "
                                     "chastichnyh rezultatov. Dedup ciranykh. Bez vydumok:\n\n"
                                     + reduce_input)}])
    return validate_protocol(_extract_json(reply))
