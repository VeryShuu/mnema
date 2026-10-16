"""Sborka etapov konveyera dlya otobrazheniya v web-interfeyse.

Etapy vyvoditsya iz zhurnala SessionEvent + tekushchego statusa joba;
sostoyaniya: done | running | pending | skipped | failed.
Logika vyvoderesistente: dlya starykh jobov (bez sobytiy asr_start/llm_start
it.d.) sostoyanie vnovitsya po minimumu iz dostupnykh priznakov.
"""
from __future__ import annotations

from sqlalchemy import select

from .db import get_session
from .models import Job, Protocol, ReviewRequest, SessionEvent

STAGE_DEFS: list[tuple[str, str, str]] = [
    ("created", "Создание встречи", "Регистрация встречи в системе"),
    ("upload", "Загрузка файла", "Приём архивной записи (для загруженных встреч)"),
    ("recording", "Запись встречи", "Захват аудио с видеоконференции"),
    ("prep", "Подготовка аудио", "Распаковка в wav/mp3 и сегментация речи (VAD)"),
    ("asr", "Распознавание речи", "Транскрибирование сегментов (ASR-движок)"),
    ("speaker", "Анализ голосов", "Диаризация — разделение на спикеров"),
    ("voiceprint", "Узнавание спикеров", "Сопоставление кластеров с реестром голосов"),
    ("steno", "Сборка стенограммы", "Формирование steno.md из сегментов"),
    ("review", "Уточнение спикеров", "Проверка нераспознанных голосов человеком"),
    ("split", "Разделение встреч", "Поиск границ между совещаниями в одной записи"),
    ("llm", "Анализ LLM", "Структурирование протокола через LLM"),
    ("done", "Протокол готов", "Итоговый протокол сформирован"),
]


def build_stages(job_id: int) -> list[dict]:
    with get_session() as s:
        job = s.get(Job, job_id)
        events = s.scalars(select(SessionEvent).where(
            SessionEvent.job_id == job_id).order_by(SessionEvent.ts.desc())).all()
        proto = s.scalars(select(Protocol).where(
            Protocol.job_id == job_id)).first()
        reviews_open = bool(s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == job_id,
            ReviewRequest.status == "pending")).first())
        reviews_answered = bool(s.scalars(select(ReviewRequest).where(
            ReviewRequest.job_id == job_id,
            ReviewRequest.status.in_(
                ("confirmed", "timeout", "excluded", "superseded")))).first())

    first: dict[str, dict] = {}
    for e in reversed(events):
        if e.event not in first:
            first[e.event] = {"ts": e.ts, "detail": e.detail or ""}

    status = job.status if job else "scheduled"
    is_upload = job is not None and job.source == "upload"
    is_split_child = job is not None and job.source == "split"
    failed = status == "failed"
    failed_stage = _failed_stage(job.fail_reason if job else None, first)
    proto_structured = proto is not None and not (
        proto.json_data or {}).get("transcript_only") and (
        proto.md_path or proto.docx_path)
    steno_exists = proto is not None or "steno_done" in first

    def has(*names: str) -> bool:
        return any(n in first for n in names)

    vad_done = has("vad_done")
    asr_done = has("asr_done")
    diar_done = has("diar_done")
    vp_done = has("voiceprint_done")

    states: dict[str, str] = {}
    states["created"] = "done"
    if is_upload:
        if has("upload_complete"):
            states["upload"] = "done"
        elif failed:
            states["upload"] = "failed" if failed_stage == "upload" else "running"
        elif status == "uploading":
            states["upload"] = "running"
        else:
            states["upload"] = "pending"
    else:
        states["upload"] = "skipped"
    if is_upload:
        states["recording"] = "skipped"
    elif is_split_child:
        states["recording"] = "skipped"  # запись «унаследована» от родителя
    elif failed_stage == "recording":
        states["recording"] = "failed"
    elif has("media", "joined", "recording_started") or status in (
            "recording", "processing", "awaiting_llm", "awaiting_review", "done"):
        states["recording"] = "done"
    elif status == "launching":
        states["recording"] = "running"
    else:
        states["recording"] = "pending"

    if vad_done:
        states["prep"] = "done"
    elif failed and failed_stage == "prep":
        states["prep"] = "failed"
    elif status == "processing":
        states["prep"] = "running"
    else:
        states["prep"] = "pending"

    if asr_done:
        states["asr"] = "done"
    elif failed and failed_stage == "asr":
        states["asr"] = "failed"
    elif status == "processing" and vad_done:
        states["asr"] = "running"
    else:
        states["asr"] = "pending"

    if diar_done:
        states["speaker"] = "done"
    elif failed and failed_stage == "speaker":
        states["speaker"] = "failed"
    elif status == "processing" and vad_done:
        states["speaker"] = "running"
    else:
        states["speaker"] = "pending"

    if vp_done:
        states["voiceprint"] = "done"
    elif failed_stage == "speaker":
        states["voiceprint"] = "failed"
    elif diar_done and (status in ("awaiting_review", "done") or reviews_open):
        states["voiceprint"] = "done"
    elif status == "processing" and diar_done:
        states["voiceprint"] = "running"
    else:
        states["voiceprint"] = "pending"

    if steno_exists:
        states["steno"] = "done"
    elif failed and failed_stage == "steno":
        states["steno"] = "failed"
    elif status == "processing" and asr_done:
        states["steno"] = "running"
    else:
        states["steno"] = "pending"

    if reviews_open or status == "awaiting_review":
        states["review"] = "running"
    elif reviews_answered or has("review_resolved", "reviews_done"):
        states["review"] = "done"
    elif status == "done":
        states["review"] = "skipped"
    else:
        states["review"] = "pending"

    split_decided = job is not None and job.split_json is not None
    if has("split_done"):
        states["split"] = "done"
    elif has("split_skipped"):
        states["split"] = "skipped"
    elif is_split_child:
        states["split"] = "skipped"  # потомок — результат разделения, сам не делится
    elif split_decided:
        sj = job.split_json or {}
        if sj.get("partial"):
            states["split"] = "failed"  # применение границ сорвалось на середине
        elif sj.get("skipped"):
            states["split"] = "skipped"
        else:
            states["split"] = "done"
    elif failed and (failed_stage == "split" or has("split_error")):
        states["split"] = "failed"
    elif status == "processing" and (has("steno_done") or steno_exists) \
            and not has("split_start"):
        states["split"] = "running"
    elif has("split_start") and status == "processing":
        states["split"] = "running"
    elif has("llm_done", "done") or status == "done":
        states["split"] = "skipped"  # «старый job»: этап введён позже обработки
    else:
        states["split"] = "pending"

    # LLM idet POSLE opredeleniya golosov: pri otkrytom review on eshche nie
    # zapuskalsya (pending), krome starykh jobov s uzhe gotovym protokolom.
    if proto_structured or has("llm_done") or (
            status in ("awaiting_review", "done")) and proto:
        states["llm"] = "done"
    elif status == "awaiting_llm":
        states["llm"] = "running"
    elif failed and failed_stage == "llm":
        states["llm"] = "failed"
    elif status == "awaiting_review" and reviews_open:
        states["llm"] = "pending"
    elif status == "processing" and has("llm_start"):
        states["llm"] = "running"
    else:
        states["llm"] = "pending"

    if status == "done":
        states["done"] = "done"
    elif failed:
        states["done"] = "failed"
    else:
        states["done"] = "pending"

    stages = []
    for key, title, desc in STAGE_DEFS:
        st = states.get(key, "pending")
        ts = None
        detail = None
        if st == "done":
            ev = _pick(first, _done_events(key, vad_done, asr_done, diar_done,
                                           vp_done, steno_exists, proto_structured))
            if ev:
                ts = ev["ts"]
        elif st == "failed":
            ev = _pick(first, ("asr_error", "speaker_error", "structure_error",
                               "llm_error", "split_error"))
            if ev:
                ts, detail = ev["ts"], (ev["detail"] or None)
        stages.append({"key": key, "title": title, "description": desc,
                       "state": st, "ts": ts, "detail": detail})
    return stages


def running_stage_title(job_id: int) -> str | None:
    """Zagolovok tekushchego (running) etapa dlya podrobnogo statusa."""
    for st in build_stages(job_id):
        if st["state"] == "running":
            return st["title"]
    return None


def _done_events(key: str, vad_done: bool, asr_done: bool, diar_done: bool,
                 vp_done: bool, steno_exists: bool, proto_structured: bool):
    return {
        "created": ("created",),
        "upload": ("upload_complete",),
        "recording": ("media", "joined", "recording_started"),
        "prep": ("vad_done",),
        "asr": ("asr_done",),
        "speaker": ("diar_done",),
        "voiceprint": ("voiceprint_done",),
        "steno": ("steno_done",),
        "llm": ("llm_done",),
        "review": ("reviews_done", "review_resolved"),
        "split": ("split_done",),
        "done": ("status:done", "status->done"),
    }.get(key, ())


def _pick(ev: dict, names) -> dict | None:
    for n in names:
        if n in ev:
            return ev[n]
    return None


def _failed_stage(fail_reason: str | None, ev: dict) -> str | None:
    if not fail_reason:
        return None
    direct = {"asr_error": "asr", "llm_invalid_json": "llm",
              "llm_unavailable": "llm", "llm_error": "llm",
              "speaker_error": "voiceprint", "vad_error": "prep",
              "audio_error": "prep", "upload_failed": "upload",
              "launch_failed": "recording", "join_failed": "recording",
              "split_error": "split"}
    if fail_reason in direct:
        return direct[fail_reason]
    txt = fail_reason.lower()
    for token, stage in (("asr", "asr"), ("llm", "llm"), ("speaker", "voiceprint"),
                         ("vad", "prep"), ("audio", "prep"), ("upload", "upload"),
                         ("launch", "recording"), ("zoom", "recording"),
                         ("join", "recording")):
        if token in txt:
            return stage
    if txt in ("processing_error", "processing"):
        if "llm_start" in ev:
            return "llm"
        if "asr_done" in ev:
            return "steno"
        if "vad_done" in ev and "diar_done" not in ev:
            return "speaker"
        if "vad_done" not in ev:
            return "prep"
    return None
