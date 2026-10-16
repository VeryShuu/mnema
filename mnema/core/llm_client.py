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


_THINK_REJECTED = False  # server ne znaet parametr think — bolshe ne peredavat'


def _thinking_enabled() -> bool:
    """razmyshleniya modeli: po umolchaniyu minimum (off)."""
    return os.environ.get("LLM_THINKING", "off").strip().lower() in (
        "on", "1", "true", "yes")


def llm_model() -> str:
    m = os.environ.get("LLM_MODEL", "").strip()
    allow = os.environ.get("LLM_MODEL_ALLOWLIST", "").strip()
    if allow and m not in [x.strip() for x in allow.split(",")]:
        raise LLMError(f"LLM_MODEL {m} nie v ALLOWLIST")
    return m or "llama3-saiga:32b"


def _post_chat(payload: dict):
    """Odin POST /chat/completions v exklyuzivnom LLM-zamke (odin analiz za
    raz; ozhidanie — FIFO-ochered'yu). Bez redis zamyok — no-op."""
    from .llm_gate import LLMGateTimeout, llm_turn
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with llm_turn():
            return httpx.post(f"{base}/chat/completions", headers=headers,
                              json=payload, timeout=600.0)
    except LLMGateTimeout as e:
        # zhdali ocheredi slishkom dolgo: eto oshibka LLM, a ne konveyera
        # (awaiting_llm / split_skipped, mozhno rerunut')
        raise LLMError(str(e)) from e


def _chat(messages: list[dict], retries: int = 2) -> str:
    global _THINK_REJECTED
    import time
    thinking_on = _thinking_enabled()
    payload: dict = {"model": llm_model(), "messages": messages,
                     "temperature": 0.1}
    if not _THINK_REJECTED:
        # dva formata odnovremenno: ollama (`think`) i OpenAI-style
        # (`thinking.type`); neznakomye poly servery ignoriiruyut, a strogiy
        # 400 s «think» otlavlivaeetsya nizhe i flag bolshe ne peredaetsya
        payload["think"] = thinking_on
        payload["thinking"] = {"type": "enabled" if thinking_on else "disabled"}
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = _post_chat(payload)
            if r.status_code >= 400:
                if ("think" in payload and r.status_code == 400
                        and "think" in r.text):
                    # server (ne ollama) ne priemaet flag mysley — prodolzhaem
                    # bez nego, chtoby ne lomayut'sya na kazhdom zaprase
                    _THINK_REJECTED = True
                    payload.pop("think")
                    payload.pop("thinking")
                    last_err = LLMError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
                    continue
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


def structure_transcript(transcript: str, topic: str = "", roster: str = "") -> dict:
    """Polnaya stenogramma -> protocol JSON. Kosol'sl'zye okna esli > konteksta.

    roster — spravka 'FIО — dolzhnost' uchastnikov (dlya disambiguacii, v
    protokole ne otrazhaetsya)."""
    roster = (roster or "").strip()
    max_chars = int(os.environ.get("LLM_WINDOW_CHARS", "24000"))
    if len(transcript) <= max_chars:
        user = f"TEMA: {topic}\n\nSTENOGRAMMA:\n{transcript}"
        if roster:
            user += (f"\n\nSPRAVOCHNIK UCHASTNIKOV (FIО — dolzhnost, dlya poni"
                     f"maniya, kto chto skazal; v protokole dolzhnosti ne "
                     f"ukazyvai):\n{roster}")
        reply = _chat([{"role": "system", "content": SYSTEM_PROMPT},
                       {"role": "user", "content": user}])
        return validate_protocol(_extract_json(reply))
    return _map_reduce(transcript, topic, max_chars, roster)


def _map_reduce(transcript: str, topic: str, max_chars: int, roster: str = "") -> dict:
    windows = []
    roster_block = (f"\n\nSPRAVOCHNIK UCHASTNIKOV (FIО — dolzhnost):\n{roster}"
                    if roster else "")
    step = int(max_chars * 0.8)
    i = 0
    while i < len(transcript):
        windows.append(transcript[i:i + max_chars])
        i += step
    partials: list[dict] = []
    for idx, w in enumerate(windows):
        user = (f"OKNO {idx + 1} iz {len(windows)}. Vytashi iz eta fragmanta "
                f"po skHEME (chastichno): resheniya, zadachi, otkrytye voprosy, "
                f"upomyanutykh lyudey.\n\n{w}" + roster_block)
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


SPLIT_SYSTEM_PROMPT = (
    "Ty analysiruesh stenogramu ONE audiozapisi s neskol'kimi vstrechami podrjad. "
    "V stenograme metki [ТИШИНА HH:MM:SS — HH:MM:SS (N c)] — dl'nye pauzy mezhdu "
    "replikami: Granada vstrechi chashche vse-go v konce ili sleza za takoy metkoy. "
    "Naydi GRANICY mezhdu raznymi sovieshchaniyami: cmera, sogovorniki, "
    "poviestka ili privetstvie osnovy. Vozvrashi ISTORIMO validny JSON bez "
    "poyasneniy: {\"boundaries\": [{\"start_ts\": \"CHCH:MM:SS\", "
    "\"confidence\": 0..1, \"topic_after\": \"tema vstrechi posle granicy\"}]}. "
    "Esli v zapisi ODNA vstrecha — Verni {\"boundaries\": []}. "
    "start_ts — vremya pervoy repliki NOVOY vstrechi iz stenogrammy. "
    "Ne vydumyivay granicy: esli uspehen nie uveren — ne vklyuchay yeye."
)


def parse_ts(value: object) -> float:
    """CHCH:MM:SS (MM/SS dopuskeny dvuznachnymi, s — desyatichnaya) -> sekundy.

    LLM often writes 'M:SS' or 'HH:MM:SS,m'. Raises LLMError on unparsable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < 0:
            raise LLMError(f"otricatel'noye vremya: {value}")
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise LLMError(f"vremya nie stroka: {value!r}")
    text = value.strip().replace(",", ".")
    parts = [p for p in text.split(":")]
    if not 1 <= len(parts) <= 3 or any(not p.strip() for p in parts):
        raise LLMError(f"nie format vremeni: {value!r}")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise LLMError(f"nie format vremeni: {value!r}") from e
    if any(n < 0 for n in nums):
        raise LLMError(f"otricatel'noye vremya: {value!r}")
    for n in nums[1:]:
        if n >= 6000:
            raise LLMError(f"nie format vremeni: {value!r}")
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    if secs > 30 * 3600:
        raise LLMError(f"vremya vnye zdravogo diapazona: {value!r}")
    return secs


def _confidence(value: object) -> float:
    try:
        c = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return c if 0.0 <= c <= 1.0 else 0.0


def _compress_line(line: str, limit: int = 120) -> str:
    if len(line) <= limit:
        return line
    return line[:limit].rstrip() + "…"


def boundaries_windows(transcript: str, window_chars: int | None = None
                       ) -> list[tuple[int, int]]:
    """Okna [start_offset, end_offset) s perekrytiyem 10% dlya skol'zyashchego poiska."""
    n = len(transcript)
    if window_chars is None:
        window_chars = int(os.environ.get("SPLIT_WINDOW_CHARS", "40000") or 40000)
    if n <= window_chars:
        return [(0, n)]
    overlap = max(1, int(window_chars * 0.1))
    step = max(1, window_chars - overlap)
    out: list[tuple[int, int]] = []
    i = 0
    while i < n:
        out.append((i, min(i + window_chars, n)))
        if i + window_chars >= n:
            break
        i += step
    return out


def detect_meeting_boundaries(transcript: str, topic: str = "",
                              duration_s: float = 0.0) -> list[dict]:
    """Odna zapis' -> spisanok granicy mezhdu vstrechami (LLM po stenogramme).

    Vozvrashaet [] esli granicy nie naydeny. Kazhdaya granica:
    {t: sek, confidence: 0..1, topic_after: str}. Pri stenograme she
    SPLIT_WINDOW_CHARS — skol'zyashchiye okna s perekrytiyem, kandidaty
    meryatsya po blizosti (<60 c — berotsya s bol'shey confidence)."""
    max_chars = int(os.environ.get("SPLIT_WINDOW_CHARS", "40000") or 40000)
    lines = [_compress_line(ln) for ln in transcript.splitlines() if ln.strip()]
    body = "\n".join(lines) or (transcript or "")
    candidates: list[dict] = []
    windows = boundaries_windows(body, max_chars)
    ok_windows = 0
    last_err: LLMError | None = None
    for lo, hi in windows:
        chunk = body[lo:hi]
        user = (f"TEMA PREDPOLOZhitel'no: {topic or '(neizvestna)'}\n"
                f"DLITEL'NOST' ZAPISI: {duration_s:.0f} s\n\n"
                f"STENOGRAMMA (fragment):\n{chunk}")
        try:
            reply = _chat([{"role": "system", "content": SPLIT_SYSTEM_PROMPT},
                           {"role": "user", "content": user}])
            data = _extract_json(reply)
            ok_windows += 1
        except LLMError as e:
            last_err = e
            continue
        for b in data.get("boundaries") or []:
            if not isinstance(b, dict):
                continue
            try:
                t = parse_ts(b.get("start_ts"))
            except LLMError:
                continue
            candidates.append({"t": t,
                               "confidence": _confidence(b.get("confidence")),
                               "topic_after": str(b.get("topic_after") or "").strip()})
    if windows and ok_windows == 0 and last_err is not None:
        # vse okna padeni — ete nie "granicy ne naydeny", a sbay detekcii
        raise last_err
    merged = merge_boundary_candidates(candidates)
    if duration_s > 0:
        merged = [b for b in merged
                  if 0.0 < b["t"] < duration_s and duration_s - b["t"] > 30.0]
    return merged


def merge_boundary_candidates(cands: list[dict], min_gap_s: float = 60.0
                              ) -> list[dict]:
    """Kandidaty iz perekryvayushchikhsya okon: blizkie (<min_gap_s) sliyayutsya,
    peryodayetsya bolshej confidence. Rezultat — monotonno vozrastayushchiy."""
    out: list[dict] = []
    for c in sorted(cands, key=lambda c: c["t"]):
        if out and c["t"] - out[-1]["t"] < min_gap_s:
            if c["confidence"] > out[-1]["confidence"]:
                out[-1] = c
            continue
        out.append(dict(c))
    return out

