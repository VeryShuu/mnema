"""Часовые пояса: хранение — UTC, отображение и ввод оператора — локальный пояс.

APP_TZ (по умолчанию Europe/Moscow) — пояс веб-интерфейса и уведомлений.
Наивные datetime из forms (datetime-local) трактуются как локальное время.
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

_FALLBACK = "Europe/Moscow"


def app_tz() -> ZoneInfo:
    name = (os.environ.get("APP_TZ") or _FALLBACK).strip() or _FALLBACK
    return ZoneInfo(name)


def app_tz_label() -> str:
    offset = dt.datetime.now(app_tz()).strftime("%z")
    return f"{app_tz().key} UTC{offset[:3]}:{offset[3:]}"


def to_local(value: dt.datetime | None) -> dt.datetime | None:
    """UTC (или aware) ->aware datetime в локальном поясе приложения."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(app_tz())


def local_to_utc(value: dt.datetime) -> dt.datetime:
    """Наивный datetime = локальное время оператора -> aware UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=app_tz())
    return value.astimezone(dt.timezone.utc)


def local_utc_offset() -> str:
    """Смещение локального пояса в формате ISO, напр. '+03:00'."""
    return dt.datetime.now(app_tz()).strftime("%z")[:3] + ":" + \
        dt.datetime.now(app_tz()).strftime("%z")[3:5]


def fmt_local(value: dt.datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    local = to_local(value)
    return local.strftime(fmt) if local else "—"
