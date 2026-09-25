"""Testy filtra human_size dlya odinits izmerezniya failov."""
import os

os.environ.setdefault("ZOOM_PROFILE_DIR", "/tmp/test-zp")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("NOTIFY_EMAIL_TO", "ops@example.ru")
os.environ.setdefault("DATA_DIR", "/tmp/test-data")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")

from mnema.api.main import human_size


def test_bytes():
    assert human_size(512) == "512 Б"


def test_kilobytes():
    assert human_size(223574 * 1024) == "218.3 МБ"


def test_megabytes_rounding_drops_dot_zero():
    assert human_size(1024 * 1024) == "1 МБ"


def test_gigabytes():
    assert human_size(3 * 1024**3) == "3 ГБ"


def test_none_and_zero_return_placeholder():
    assert human_size(None) == "?"
    assert human_size(0) == "?"
