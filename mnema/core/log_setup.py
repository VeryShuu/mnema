"""Единая настройка ходовых логов Mnema (api, worker).

Соглашение: все ходовые сообщения — на английском, через стандартный
logging. Формат единый для всех процессов; уровень — LOG_LEVEL (default INFO).
Модули берут логгер так: ``log = logging.getLogger("mnema.<component>")``.
CLI-утилиты (registry, speaker_backfill) печатают результат в stdout/stderr —
это вывод утилиты, не ходовой лог, и через этот модуль не идёт.
"""
from __future__ import annotations

import logging
import os
import sys

FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

_configured = False


def setup_logging(component: str) -> logging.Logger:
    """Настраивает root handler (идемпотентно), возвращает сервисный логгер."""
    global _configured
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    if not _configured:
        logging.basicConfig(stream=sys.stdout, level=level, format=FORMAT,
                            force=False)
        _configured = True
    elif logging.root.level != level:
        logging.root.setLevel(level)
    return logging.getLogger(f"mnema.{component}")
