"""
config.py — управление настройками Forum Rescue Archive.

Все параметры хранятся в одном JSON-файле (config.json), чтобы их можно было
редактировать через Streamlit UI и чтобы настройки переживали перезапуск
программы. При отсутствии файла создаются безопасные значения по умолчанию
(консервативные задержки, последовательная работа, тестовый режим выключен).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Подконфигурации
# ---------------------------------------------------------------------------

@dataclass
class ForumConfig:
    """Параметры целевого форума."""
    base_url: str = ""                 # например: https://example-forum.ru
    encoding: str = "utf-8"
    session_cookie: Optional[str] = None   # если форум требует логин
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 ForumRescueArchive/1.0"
    )


@dataclass
class ThrottleConfig:
    """Задержки между запросами. Скорость — не приоритет, надежность — приоритет."""
    min_delay_seconds: float = 20.0
    max_delay_seconds: float = 40.0


@dataclass
class BackoffConfig:
    """
    Стратегия ожидания при ошибках сервера (502/503/504/500/timeout).

    backoff_schedule_seconds — список интервалов ожидания по мере роста
    error_streak (счетчика последовательных ошибок сервера). Когда список
    заканчивается — используется последнее (максимальное) значение повторно.

    По умолчанию соответствует примеру из ТЗ:
        1-я ошибка  -> 10 минут
        2-я ошибка  -> 30 минут
        3-я ошибка  -> 1 час
        4-я и далее -> 2 часа (повторяется)
    """
    backoff_schedule_seconds: list[int] = field(
        default_factory=lambda: [600, 1800, 3600, 7200]
    )
    # Через сколько последовательных успешных запросов подряд сбрасывать
    # error_streak обратно в 0 (постепенное "доверие" серверу восстанавливается
    # не мгновенно после одного успеха, чтобы не дергать backoff туда-сюда).
    reset_streak_after_successes: int = 3


@dataclass
class RetryConfig:
    """Ограничения на повторные попытки для ОДНОЙ конкретной задачи (не сервера в целом)."""
    max_attempts_per_task: int = 20        # после этого задача помечается 'failed' окончательно
    treat_404_410_as_missing: bool = True  # не ошибка сервера, а "страница отсутствует"


@dataclass
class PathsConfig:
    """Пути к архиву, базе данных, логам и метаданным."""
    archive_root: str = "Archive"
    db_filename: str = "queue.db"          # будет лежать в metadata/
    metadata_dirname: str = "metadata"
    logs_dirname: str = "logs"
    raw_html_dirname: str = "metadata/raw_html"

    def db_path(self) -> str:
        return os.path.join(self.archive_root, self.metadata_dirname, self.db_filename)

    def metadata_dir(self) -> str:
        return os.path.join(self.archive_root, self.metadata_dirname)

    def logs_dir(self) -> str:
        return os.path.join(self.archive_root, self.logs_dirname)

    def raw_html_dir(self) -> str:
        return os.path.join(self.archive_root, self.raw_html_dirname)

    def progress_json_path(self) -> str:
        return os.path.join(self.metadata_dir(), "progress_state.json")

    def forum_structure_path(self) -> str:
        return os.path.join(self.metadata_dir(), "forum_structure.json")

    def report_json_path(self) -> str:
        return os.path.join(self.metadata_dir(), "archive_report.json")

    def report_html_path(self) -> str:
        return os.path.join(self.metadata_dir(), "archive_report.html")


@dataclass
class ModeConfig:
    """Режимы работы: 1 = 'Спасение текста', 2 = 'Полный архив'."""
    mode: int = 1   # 1 или 2

    def priority_ceiling(self) -> int:
        """Задачи с priority <= этого значения попадают в очередь текущего режима."""
        return 1 if self.mode == 1 else 2


@dataclass
class TestModeConfig:
    """
    Тестовый режим — обязательный шаг перед полным запуском (см. требование
    пользователя): ограничиваем объем работы, чтобы проверить структуру
    конкретного форума phpBB и реальную нагрузку на сервер перед тем, как
    запускать архивацию всех ~59 000 сообщений.
    """
    enabled: bool = False
    max_sections: Optional[int] = 1
    max_topics_per_section: Optional[int] = 3
    max_pages_per_topic: Optional[int] = 2
    max_total_tasks: Optional[int] = 50   # общий предохранитель


# ---------------------------------------------------------------------------
# Главный конфиг
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    forum: ForumConfig = field(default_factory=ForumConfig)
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    backoff: BackoffConfig = field(default_factory=BackoffConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    test_mode: TestModeConfig = field(default_factory=TestModeConfig)

    # -- сериализация --------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "forum": asdict(self.forum),
            "throttle": asdict(self.throttle),
            "backoff": asdict(self.backoff),
            "retry": asdict(self.retry),
            "paths": asdict(self.paths),
            "mode": asdict(self.mode),
            "test_mode": asdict(self.test_mode),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        return cls(
            forum=ForumConfig(**data.get("forum", {})),
            throttle=ThrottleConfig(**data.get("throttle", {})),
            backoff=BackoffConfig(**data.get("backoff", {})),
            retry=RetryConfig(**data.get("retry", {})),
            paths=PathsConfig(**data.get("paths", {})),
            mode=ModeConfig(**data.get("mode", {})),
            test_mode=TestModeConfig(**data.get("test_mode", {})),
        )


DEFAULT_CONFIG_PATH = "config.json"


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """
    Загружает конфигурацию из JSON. Если файла нет — создает его со
    значениями по умолчанию и возвращает.
    """
    if not os.path.exists(path):
        cfg = AppConfig()
        save_config(cfg, path)
        return cfg

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return AppConfig.from_dict(data)


def save_config(config: AppConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
    """
    Сохраняет конфигурацию атомарно (через временный файл + os.replace),
    чтобы при сбое во время записи не повредить существующий config.json.
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def ensure_directories(config: AppConfig) -> None:
    """Создает всю структуру папок архива, если ее еще нет (см. п.5 ТЗ)."""
    paths = config.paths
    dirs = [
        paths.archive_root,
        paths.metadata_dir(),
        paths.logs_dir(),
        paths.raw_html_dir(),
        os.path.join(paths.archive_root, "forum", "sections"),
        os.path.join(paths.archive_root, "attachments"),
        os.path.join(paths.archive_root, "images"),
        os.path.join(paths.archive_root, "avatars"),
        os.path.join(paths.archive_root, "smilies"),
        os.path.join(paths.archive_root, "css"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
