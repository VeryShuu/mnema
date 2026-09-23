"""Тесты часовых поясов: хранение UTC, ввод/отображение — Europe/Moscow."""
import datetime as dt

import pytest

from mnema.core.tz import (app_tz, app_tz_label, fmt_local, local_to_utc,
                           local_utc_offset, to_local)


@pytest.fixture(autouse=True)
def _msk(monkeypatch):
    monkeypatch.setenv("APP_TZ", "Europe/Moscow")


def test_to_local_naive_utc_becomes_msk():
    naive = dt.datetime(2026, 9, 23, 13, 0)  # kak SQLite otdaёт UTC
    local = to_local(naive)
    assert local.hour == 16
    assert local.utcoffset() == dt.timedelta(hours=3)


def test_to_local_aware_utc_becomes_msk():
    aware = dt.datetime(2026, 9, 23, 13, 0, tzinfo=dt.timezone.utc)
    assert to_local(aware).strftime("%H:%M") == "16:00"


def test_to_local_none():
    assert to_local(None) is None


def test_local_to_utc_naive_is_msk():
    naive = dt.datetime(2026, 9, 23, 16, 0)
    utc = local_to_utc(naive)
    assert utc.tzinfo is dt.timezone.utc
    assert utc == dt.datetime(2026, 9, 23, 13, 0, tzinfo=dt.timezone.utc)


def test_local_to_utc_aware_passthrough():
    aware = dt.datetime(2026, 9, 23, 16, 0, tzinfo=dt.timezone.utc)
    assert local_to_utc(aware) == aware


def test_fmt_local_and_none():
    assert fmt_local(dt.datetime(2026, 9, 23, 13, 5)) == "23.09.2026 16:05"
    assert fmt_local(None) == "—"


def test_app_tz_env_override(monkeypatch):
    monkeypatch.setenv("APP_TZ", "Asia/Yekaterinburg")
    assert app_tz().key == "Asia/Yekaterinburg"
    assert to_local(dt.datetime(2026, 9, 23, 13, 0)).hour == 18


def test_app_tz_default_when_empty(monkeypatch):
    monkeypatch.setenv("APP_TZ", "")
    assert app_tz().key == "Europe/Moscow"


def test_app_tz_label():
    assert app_tz_label() == "Europe/Moscow UTC+03:00"


def test_local_utc_offset():
    assert local_utc_offset() == "+03:00"
