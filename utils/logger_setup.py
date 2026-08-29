"""
logger_setup.py — настройка логирования Forum Rescue Archive.

Требования (п.11 "Logger" в архитектуре):
    - подробные логи всех действий;
    - отдельный файл только для ошибок (logs/errors.log) — чтобы быстро
      понять, что пошло не так, не листая весь общий лог;
    - ротация файлов, чтобы логи не разрастались бесконечно за недели работы;
    - вывод в консоль для интерактивного контроля во время работы.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(logs_dir: str, level: int = logging.INFO) -> logging.Logger:
    """
    Настраивает и возвращает корневой логгер 'forum_rescue_archive'.

    Создает три обработчика:
        1. archive.log  — все события уровня >= level, с ротацией (10 МБ x 5 файлов).
        2. errors.log   — только ERROR и выше, для быстрой диагностики проблем с сервером.
        3. консоль      — для наблюдения за процессом в реальном времени.
    """
    os.makedirs(logs_dir, exist_ok=True)

    logger = logging.getLogger("forum_rescue_archive")
    logger.setLevel(level)
    logger.propagate = False

    # Если logger уже настроен (повторный вызов в той же сессии Streamlit) —
    # не плодим дублирующиеся обработчики.
    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # 1. Общий лог с ротацией
    main_handler = RotatingFileHandler(
        os.path.join(logs_dir, "archive.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    main_handler.setLevel(level)
    main_handler.setFormatter(formatter)
    logger.addHandler(main_handler)

    # 2. Отдельный лог ошибок
    error_handler = RotatingFileHandler(
        os.path.join(logs_dir, "errors.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)

    # 3. Консоль
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def get_logger() -> logging.Logger:
    """Возвращает уже настроенный логгер по имени (для использования в других модулях)."""
    return logging.getLogger("forum_rescue_archive")
