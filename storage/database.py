"""
database.py — SQLite как единственный источник истины о состоянии архивации.

Почему SQLite, а не JSON (см. обсуждение архитектуры):
    - 59 000 сообщений => десятки тысяч задач (страницы + изображения +
      вложения); нужны быстрые выборки "следующая pending-задача",
      "все retry_wait с истекшим таймером" и т.д.;
    - WAL-режим переживает падение процесса/выключение питания заметно
      надежнее, чем перезапись одного большого JSON-файла.

Таблицы:
    tasks            — очередь: что скачивать, что скачано, что повторить.
    sections         — структура форума: разделы/подразделы (заполняет Scanner).
    topics           — структура форума: темы внутри разделов.
    kv_state         — key-value: глобальный error_streak, backing_off_until,
                        агрегированные счетчики (pages_saved, images_saved и т.д.).

Человекочитаемый снимок прогресса (progress_state.json) пишется отдельной
функцией write_progress_snapshot() атомарно (temp-файл + os.replace) — это
для быстрого отображения в Streamlit UI без обращения к SQLite на каждый кадр.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from utils.logger_setup import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def make_task_id(url: str) -> str:
    """Стабильный ID задачи от URL — позволяет дедуплицировать задачи при
    повторном сканировании (не добавляем то, что уже в очереди)."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_host(url: str) -> str:
    """Хост из URL (для backoff по отдельному хосту — см. QueueManager)."""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


# Статусы задач
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_RETRY_WAIT = "retry_wait"
STATUS_SKIPPED_MISSING = "skipped_missing"

# Типы задач
TYPE_SECTION_PAGE = "section_page"
TYPE_TOPIC_PAGE = "topic_page"
TYPE_IMAGE = "image"
TYPE_ATTACHMENT = "attachment"
TYPE_AVATAR = "avatar"
TYPE_SMILEY = "smiley"
TYPE_CSS = "css"

# Приоритеты (см. ModeConfig.priority_ceiling): режим 1 берет только <= 1
PRIORITY_TEXT = 1        # section_page, topic_page
PRIORITY_RESOURCE = 2    # image, attachment, avatar, smiley, css

TYPE_PRIORITY = {
    TYPE_SECTION_PAGE: PRIORITY_TEXT,
    TYPE_TOPIC_PAGE: PRIORITY_TEXT,
    TYPE_IMAGE: PRIORITY_RESOURCE,
    TYPE_ATTACHMENT: PRIORITY_RESOURCE,
    TYPE_AVATAR: PRIORITY_RESOURCE,
    TYPE_SMILEY: PRIORITY_RESOURCE,
    TYPE_CSS: PRIORITY_RESOURCE,
}


@dataclass
class Task:
    id: str
    type: str
    url: str
    local_path: str
    status: str = STATUS_PENDING
    attempts: int = 0
    next_attempt_at: Optional[str] = None
    parent_id: Optional[str] = None
    priority: int = PRIORITY_TEXT
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    host: Optional[str] = None
    section_id: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(
            id=row["id"],
            type=row["type"],
            url=row["url"],
            local_path=row["local_path"],
            status=row["status"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            parent_id=row["parent_id"],
            priority=row["priority"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            host=row["host"] if "host" in row.keys() else None,
            section_id=row["section_id"] if "section_id" in row.keys() else None,
        )


# ---------------------------------------------------------------------------
# Подключение и инициализация
# ---------------------------------------------------------------------------

def connect_db(db_path: str) -> sqlite3.Connection:
    """Открывает соединение с SQLite в WAL-режиме (устойчивость к падению процесса)."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Создает все таблицы, если их еще нет. Безопасно вызывать многократно."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id              TEXT PRIMARY KEY,
            type            TEXT NOT NULL,
            url             TEXT NOT NULL,
            local_path      TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            parent_id       TEXT,
            priority        INTEGER NOT NULL DEFAULT 1,
            error_message   TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
        CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);

        CREATE TABLE IF NOT EXISTS sections (
            id          TEXT PRIMARY KEY,
            forum_id    TEXT NOT NULL,
            name        TEXT NOT NULL,
            url         TEXT NOT NULL,
            parent_id   TEXT,
            page_count  INTEGER DEFAULT 1,
            scanned     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS topics (
            id          TEXT PRIMARY KEY,
            section_id  TEXT NOT NULL,
            name        TEXT NOT NULL,
            url         TEXT NOT NULL,
            author      TEXT,
            page_count  INTEGER DEFAULT 1,
            scanned     INTEGER DEFAULT 0,
            FOREIGN KEY (section_id) REFERENCES sections(id)
        );

        CREATE TABLE IF NOT EXISTS kv_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS host_state (
            host                    TEXT PRIMARY KEY,
            error_streak            INTEGER NOT NULL DEFAULT 0,
            backing_off_until       TEXT,
            consecutive_successes   INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    # Миграция: колонка host появилась не с первого дня — на уже
    # существующих базах (у пользователя уже есть боевые архивы) её могло
    # не быть. ALTER TABLE ADD COLUMN безопасен и не трогает существующие
    # строки (для них host останется NULL — такие старые задачи просто не
    # участвуют в фильтрации по хосту, это не мешает работе).
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "host" not in existing_columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN host TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_host ON tasks(host)")
        logger.info("Миграция БД: добавлена колонка tasks.host (для backoff по отдельному хосту).")

    # Миграция: колонка section_id — для выборочной архивации (режим
    # "скачать только выбранные разделы"). Заполняется только у
    # section_page/topic_page (Scanner знает section_id в момент
    # постановки задачи); у ресурсов (картинки/CSS/...) остаётся NULL —
    # им фильтр по разделу не нужен, они и так скачиваются только если
    # родительская текстовая страница уже была скачана. На старых базах
    # существующие задачи получат NULL — это не ломает работу, просто
    # такие задачи не участвуют в фильтрации по разделу (равносильно
    # "раздел неизвестен", см. get_next_task).
    if "section_id" not in existing_columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN section_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_section ON tasks(section_id)")
        logger.info("Миграция БД: добавлена колонка tasks.section_id (для выборочной архивации по разделам).")

    conn.commit()
    logger.info("База данных инициализирована (WAL-режим).")


# ---------------------------------------------------------------------------
# Key-Value состояние (error_streak, backing_off_until, счетчики)
# ---------------------------------------------------------------------------

def kv_get(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def kv_increment(conn: sqlite3.Connection, key: str, amount: int = 1) -> int:
    current = int(kv_get(conn, key, "0"))
    new_value = current + amount
    kv_set(conn, key, str(new_value))
    return new_value


def get_section_path(conn: sqlite3.Connection, section_id: str) -> list[str]:
    """Строит путь названий от корня до раздела (для структуры папок на диске).
    Используется и Scanner (при постановке задач), и Downloader (при
    обнаружении новых страниц темы) — поэтому вынесено сюда как общая
    операция над схемой БД."""
    path: list[str] = []
    current_id = section_id
    seen = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        row = conn.execute(
            "SELECT name, parent_id FROM sections WHERE id = ?", (current_id,)
        ).fetchone()
        if row is None:
            break
        path.insert(0, row["name"])
        current_id = row["parent_id"]
    return path


def get_topic(conn: sqlite3.Connection, topic_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()


def increment_topic_page_count(conn: sqlite3.Connection, topic_id: str) -> int:
    """Увеличивает счетчик известных страниц темы на 1 и возвращает новое
    значение — используется Downloader для нумерации вновь обнаруженных
    страниц темы (page_002.html, page_003.html, ...)."""
    row = get_topic(conn, topic_id)
    new_count = (row["page_count"] if row else 1) + 1
    conn.execute("UPDATE topics SET page_count = ? WHERE id = ?", (new_count, topic_id))
    conn.commit()
    return new_count


# ---------------------------------------------------------------------------
# QueueManager
# ---------------------------------------------------------------------------

class QueueManager:
    """
    Единственный компонент, решающий "что скачивать дальше".

    Downloader ничего не знает про разделы/темы — он получает Task и просто
    выполняет ее, а результат сообщает обратно через mark_task_done /
    mark_task_failed / mark_task_skipped_missing.
    """

    def __init__(self, conn: sqlite3.Connection, backoff_schedule: list[int],
                 reset_streak_after_successes: int = 3,
                 max_attempts_per_task: int = 20,
                 max_attempts_per_task_external: Optional[int] = None,
                 own_host: Optional[str] = None):
        """
        own_host — хост самого форума (например, "listbb.ru"), обычно
        extract_host(config.forum.base_url). Нужен, чтобы отличать "у
        самого форума проблемы" (это по-прежнему считается и в глобальный
        error_streak — старое поведение, ничего не изменилось) от "ошибся
        сторонний хостинг картинок" (считается ТОЛЬКО в backoff этого
        конкретного хоста, глобальный streak не растёт).

        Если не передать (None, по умолчанию) — старое поведение
        сохраняется полностью: любая ошибка, с любого хоста, считается в
        общий error_streak, как было до появления backoff по хостам. Это
        осознанный дефолт для обратной совместимости (например, в тестах,
        где own_host сейчас не передаётся).

        max_attempts_per_task_external — отдельный (обычно более короткий)
        лимит попыток для задач со сторонних хостов (host != own_host).
        Если не передан (None) — используется max_attempts_per_task, как
        и раньше (обратная совместимость).
        """
        self.conn = conn
        self.backoff_schedule = backoff_schedule
        self.reset_streak_after_successes = reset_streak_after_successes
        self.max_attempts_per_task = max_attempts_per_task
        self.max_attempts_per_task_external = (
            max_attempts_per_task_external
            if max_attempts_per_task_external is not None
            else max_attempts_per_task
        )
        self.own_host = own_host

    # -- добавление задач ------------------------------------------------

    def add_task(self, type_: str, url: str, local_path: str,
                  parent_id: Optional[str] = None,
                  priority: Optional[int] = None,
                  section_id: Optional[str] = None) -> str:
        """
        Добавляет задачу в очередь. Идемпотентно: если задача с таким же
        URL уже существует (тот же id), повторно не добавляется — это и
        есть механизм "не скачивать повторно уже сохраненное" (п.3 ТЗ).

        section_id — раздел, к которому относится задача (для
        выборочной архивации, см. get_next_task). Указывается только
        для section_page/topic_page — Scanner знает раздел в момент
        постановки. Для ресурсов (картинки/CSS/...) не указывается: они
        и так скачиваются только после родительской текстовой страницы,
        отдельная фильтрация по разделу им не нужна.
        """
        task_id = make_task_id(url)
        existing = self.conn.execute(
            "SELECT id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if existing:
            return task_id

        now = utcnow_iso()
        prio = priority if priority is not None else TYPE_PRIORITY.get(type_, PRIORITY_RESOURCE)
        host = extract_host(url)
        self.conn.execute(
            """
            INSERT INTO tasks (id, type, url, local_path, status, attempts,
                                next_attempt_at, parent_id, priority,
                                error_message, created_at, updated_at, host,
                                section_id)
            VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (task_id, type_, url, local_path, STATUS_PENDING, parent_id, prio, now, now, host, section_id),
        )
        self.conn.commit()
        return task_id

    # -- получение следующей задачи --------------------------------------

    def get_next_task(self, priority_ceiling: int = PRIORITY_TEXT,
                       allowed_section_ids: Optional[list[str]] = None) -> Optional[Task]:
        """
        Возвращает следующую задачу для выполнения с учетом:
            - фильтра режима (priority_ceiling: 1 = только текст, 2 = все);
            - глобального backoff сервера (если сервер сейчас "отдыхает" —
              возвращать None, даже если есть pending-задачи) — это старое
              поведение, не изменилось;
            - backoff ПО ОТДЕЛЬНОМУ ХОСТУ (новое) — если конкретный хост
              (например, мёртвый сторонний фотохостинг) сейчас "отдыхает",
              его задачи временно не выдаются, но задачи с ДРУГИХ хостов
              (включая сам форум) продолжают выдаваться как обычно;
            - retry_wait задач, у которых next_attempt_at уже наступил;
            - фильтра по выбранным разделам (allowed_section_ids, новое —
              выборочная архивация). None (по умолчанию) — без ограничения,
              поведение как раньше. Если задан список: section_page/topic_page
              берутся только если их section_id в списке; ресурсы
              (картинки/CSS/...) не имеют своего section_id — для них
              проверяется section_id их РОДИТЕЛЬСКОЙ задачи (parent_id).
              У задач без parent_id и без section_id (старые записи в базе,
              до появления этой колонки) фильтр не применяется — они
              по-прежнему выдаются, чтобы не заблокировать уже
              существующие архивы, начатые до этой возможности.

        Помечает возвращаемую задачу как in_progress перед возвратом.
        """
        if self.is_server_backing_off():
            return None

        now = utcnow_iso()
        backing_off_hosts = self._currently_backing_off_hosts()

        host_filter_sql = ""
        params: list = [priority_ceiling, now]
        if backing_off_hosts:
            placeholders = ",".join("?" for _ in backing_off_hosts)
            host_filter_sql = f"AND (host IS NULL OR host NOT IN ({placeholders}))"
            params.extend(backing_off_hosts)

        section_filter_sql = ""
        if allowed_section_ids:
            placeholders = ",".join("?" for _ in allowed_section_ids)
            # Задача разрешена, если:
            #   - её собственный section_id в списке (section_page/topic_page), ИЛИ
            #   - её parent_id указывает на задачу, чей section_id в списке
            #     (ресурсы — картинки/CSS/... наследуют раздел от родителя), ИЛИ
            #   - у неё вообще нет ни section_id, ни parent_id с известным
            #     section_id (старые задачи до появления этой колонки —
            #     не блокируем то, что уже было в очереди раньше).
            section_filter_sql = f"""
                AND (
                    section_id IN ({placeholders})
                    OR parent_id IN (
                        SELECT id FROM tasks WHERE section_id IN ({placeholders})
                    )
                    OR (section_id IS NULL AND (
                        parent_id IS NULL
                        OR parent_id NOT IN (SELECT id FROM tasks WHERE section_id IS NOT NULL)
                    ))
                )
            """
            params.extend(allowed_section_ids)
            params.extend(allowed_section_ids)

        row = self.conn.execute(
            f"""
            SELECT * FROM tasks
            WHERE priority <= ?
              AND (
                    status = 'pending'
                    OR (status = 'retry_wait' AND next_attempt_at <= ?)
                  )
              {host_filter_sql}
              {section_filter_sql}
            ORDER BY
                CASE WHEN status = 'retry_wait' THEN 0 ELSE 1 END,
                priority ASC,
                created_at ASC
            LIMIT 1
            """,
            params,
        ).fetchone()

        if row is None:
            return None

        task = Task.from_row(row)
        self.conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (STATUS_IN_PROGRESS, utcnow_iso(), task.id),
        )
        self.conn.commit()
        task.status = STATUS_IN_PROGRESS
        return task

    def host_backoff_wait_seconds(self, priority_ceiling: int = PRIORITY_TEXT,
                                   allowed_section_ids: Optional[list[str]] = None) -> Optional[float]:
        """
        Используется Downloader'ом, когда get_next_task() вернул None, а
        is_server_backing_off() — False: значит, свободные задачи есть, но
        все они принадлежат хостам, которые сейчас "отдыхают". Возвращает,
        сколько секунд подождать до ближайшего окончания backoff, или None,
        если это не тот случай (очередь просто пуста, либо есть свободные
        задачи вообще без ограничений — тогда возвращать нечего, вызывающий
        код должен просто снова вызвать get_next_task()).

        allowed_section_ids — тот же фильтр выборочной архивации, что и в
        get_next_task (см. его docstring) — нужен, чтобы не спутать
        "задачи вне выбранных разделов есть, но не считаются" с
        "задачи есть, просто хост отдыхает".
        """
        now = utcnow_iso()
        section_filter_sql = ""
        params: list = [priority_ceiling, now]
        if allowed_section_ids:
            placeholders = ",".join("?" for _ in allowed_section_ids)
            section_filter_sql = f"""
                AND (
                    section_id IN ({placeholders})
                    OR parent_id IN (
                        SELECT id FROM tasks WHERE section_id IN ({placeholders})
                    )
                    OR (section_id IS NULL AND (
                        parent_id IS NULL
                        OR parent_id NOT IN (SELECT id FROM tasks WHERE section_id IS NOT NULL)
                    ))
                )
            """
            params.extend(allowed_section_ids)
            params.extend(allowed_section_ids)

        raw_candidate = self.conn.execute(
            f"""
            SELECT 1 FROM tasks
            WHERE priority <= ?
              AND (status = 'pending' OR (status = 'retry_wait' AND next_attempt_at <= ?))
              {section_filter_sql}
            LIMIT 1
            """,
            params,
        ).fetchone()
        if raw_candidate is None:
            return None  # действительно пусто, дело не в backoff по хосту

        backing_off_hosts = self._currently_backing_off_hosts()
        if not backing_off_hosts:
            return None  # не должно происходить, но на всякий случай

        soonest: Optional[datetime] = None
        for host in backing_off_hosts:
            until_str = self._host_backing_off_until(host)
            if not until_str:
                continue
            try:
                until = datetime.fromisoformat(until_str)
            except ValueError:
                continue
            if soonest is None or until < soonest:
                soonest = until

        if soonest is None:
            return None
        remaining = (soonest - datetime.now(timezone.utc)).total_seconds()
        return max(remaining, 0.0)

    def count_deferred_by_mode(self, priority_ceiling: int) -> int:
        """
        Число задач, отложенных текущим режимом (priority выше
        priority_ceiling), а не backoff'ом или ошибкой — то есть задачи,
        которые get_next_task() никогда не вернёт, пока пользователь не
        переключит режим на более широкий (например, ресурсы вроде CSS
        в режиме 1 "Спасение текста"). Считает только незавершённые задачи
        (pending/retry_wait/in_progress) — уже done/failed/skipped_missing
        сюда не входят, т.к. они не "отложены", а обработаны.
        """
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM tasks
            WHERE priority > ?
              AND status IN (?, ?, ?)
            """,
            (priority_ceiling, STATUS_PENDING, STATUS_RETRY_WAIT, STATUS_IN_PROGRESS),
        ).fetchone()
        return row["n"]
       

    # -- завершение задач --------------------------------------------------

    def mark_task_done(self, task_id: str) -> None:
        row = self.conn.execute("SELECT host FROM tasks WHERE id = ?", (task_id,)).fetchone()
        host = row["host"] if row and "host" in row.keys() else None

        self.conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, error_message = NULL WHERE id = ?",
            (STATUS_DONE, utcnow_iso(), task_id),
        )
        self.conn.commit()
        self._register_success()
        if host:
            self._register_host_success(host)

    def mark_task_skipped_missing(self, task_id: str, note: str = "404/410 Not Found") -> None:
        """Страница реально не существует — это НЕ ошибка сервера, backoff не растет."""
        self.conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, error_message = ? WHERE id = ?",
            (STATUS_SKIPPED_MISSING, utcnow_iso(), note, task_id),
        )
        self.conn.commit()
        logger.info(f"Задача {task_id} пропущена (страница отсутствует): {note}")

    def mark_task_failed(self, task_id: str, error_message: str) -> None:
        """
        Ошибка сервера (500/502/503/504/timeout/connection error).

        В общий (глобальный) error_streak засчитывается, только если ошибся
        сам форум (host == own_host) — или если own_host вообще не задан
        (старое поведение, для обратной совместимости). Ошибка стороннего
        хостинга (host != own_host) — например, мёртвая ссылка на
        radikal.ru — считается ТОЛЬКО в backoff этого конкретного хоста,
        чтобы не приостанавливать скачивание всего остального, включая
        сам форум.

        Если превышен max_attempts_per_task — задача помечается 'failed'
        окончательно (не блокирует остальную очередь).
        """
        row = self.conn.execute(
            "SELECT attempts, host FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        host = row["host"] if row and "host" in row.keys() else None

        count_globally = self.own_host is None or host is None or host == self.own_host

        if count_globally:
            streak = self._register_failure()
        else:
            streak = int(kv_get(self.conn, "error_streak", "0"))  # для лога/возврата, не меняется
        host_streak = None
        if host:
            host_streak = self._register_host_failure(host)
        delay_seconds = self._backoff_delay_for_streak(
            host_streak if (host and not count_globally) else streak
        )
        next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()

        effective_streak = host_streak if (host and not count_globally) else streak

        task_max_attempts = (
            self.max_attempts_per_task if count_globally else self.max_attempts_per_task_external
        )

        if attempts >= task_max_attempts:
            status = STATUS_FAILED
            next_attempt = None
            logger.error(
                f"Задача {task_id} окончательно провалена после {attempts} попыток: {error_message}"
            )
        else:
            status = STATUS_RETRY_WAIT
            logger.warning(
                f"Задача {task_id} провалена (попытка {attempts}): {error_message}. "
                f"Повтор через {delay_seconds} сек. (error_streak={effective_streak})"
            )

        self.conn.execute(
            """
            UPDATE tasks
            SET status = ?, attempts = ?, next_attempt_at = ?,
                error_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, attempts, next_attempt, error_message, utcnow_iso(), task_id),
        )
        self.conn.commit()

    # -- восстановление после падения программы ---------------------------

    def reset_stuck_in_progress(self) -> int:
        """
        При старте программы: задачи, зависшие в 'in_progress' (программа
        упала посреди скачивания), возвращаются в 'pending'. Файл на диске
        в этом случае перезаписывается заново — частично скачанный файл не
        считается валидным.
        Возвращает количество восстановленных задач.
        """
        cur = self.conn.execute(
            "SELECT id FROM tasks WHERE status = ?", (STATUS_IN_PROGRESS,)
        )
        ids = [r["id"] for r in cur.fetchall()]
        if ids:
            self.conn.executemany(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                [(STATUS_PENDING, utcnow_iso(), i) for i in ids],
            )
            self.conn.commit()
            logger.warning(
                f"Восстановлено {len(ids)} задач, зависших в 'in_progress' "
                f"после предыдущего аварийного завершения."
            )
        return len(ids)

    # -- backoff сервера (глобальный, не per-task) -------------------------

    def _backoff_delay_for_streak(self, streak: int) -> int:
        if streak <= 0:
            return 0
        idx = min(streak - 1, len(self.backoff_schedule) - 1)
        return self.backoff_schedule[idx]

    def _register_failure(self) -> int:
        streak = kv_increment(self.conn, "error_streak", 1)
        kv_set(self.conn, "consecutive_successes", "0")
        delay = self._backoff_delay_for_streak(streak)
        if delay > 0:
            backing_off_until = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            kv_set(self.conn, "backing_off_until", backing_off_until)
        kv_increment(self.conn, "stat_errors", 1)
        return streak

    def _register_success(self) -> None:
        successes = kv_increment(self.conn, "consecutive_successes", 1)
        if successes >= self.reset_streak_after_successes:
            kv_set(self.conn, "error_streak", "0")
            kv_set(self.conn, "backing_off_until", "")

    def is_server_backing_off(self) -> bool:
        until = kv_get(self.conn, "backing_off_until", "")
        if not until:
            return False
        try:
            return datetime.fromisoformat(until) > datetime.now(timezone.utc)
        except ValueError:
            return False

    def backing_off_until(self) -> Optional[str]:
        until = kv_get(self.conn, "backing_off_until", "")
        return until or None

    # -- backoff по отдельному хосту (новое) --------------------------------
    #
    # Отдельно от глобального backoff выше. Смысл: если сторонний
    # фотохостинг (например, старая мёртвая ссылка radikal.ru) отвечает
    # ошибками, это не должно приостанавливать скачивание с других хостов —
    # включая сам форум. Глобальный backoff при этом продолжает работать
    # как раньше и не отменяется — он про по-настоящему общие проблемы
    # (например, сам форум лёг или сеть пропала).

    def _get_host_row(self, host: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM host_state WHERE host = ?", (host,)
        ).fetchone()

    def _register_host_failure(self, host: str) -> int:
        row = self._get_host_row(host)
        streak = (row["error_streak"] if row else 0) + 1
        delay = self._backoff_delay_for_streak(streak)
        backing_off_until = (
            (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            if delay > 0 else None
        )
        self.conn.execute(
            """
            INSERT INTO host_state (host, error_streak, backing_off_until, consecutive_successes)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(host) DO UPDATE SET
                error_streak = excluded.error_streak,
                backing_off_until = excluded.backing_off_until,
                consecutive_successes = 0
            """,
            (host, streak, backing_off_until),
        )
        self.conn.commit()
        if delay > 0:
            logger.warning(
                f"Хост {host} отдыхает {delay} сек. (error_streak={streak}) — "
                f"остальные хосты (включая форум) продолжают скачиваться."
            )
        return streak

    def _register_host_success(self, host: str) -> None:
        row = self._get_host_row(host)
        if row is None:
            return  # у хоста и так не было ошибок — нечего сбрасывать
        successes = row["consecutive_successes"] + 1
        if successes >= self.reset_streak_after_successes:
            self.conn.execute(
                "UPDATE host_state SET error_streak = 0, backing_off_until = NULL, "
                "consecutive_successes = 0 WHERE host = ?",
                (host,),
            )
        else:
            self.conn.execute(
                "UPDATE host_state SET consecutive_successes = ? WHERE host = ?",
                (successes, host),
            )
        self.conn.commit()

    def _host_backing_off_until(self, host: str) -> Optional[str]:
        row = self._get_host_row(host)
        return row["backing_off_until"] if row else None

    def _is_host_backing_off(self, host: str) -> bool:
        until = self._host_backing_off_until(host)
        if not until:
            return False
        try:
            return datetime.fromisoformat(until) > datetime.now(timezone.utc)
        except ValueError:
            return False

    def _currently_backing_off_hosts(self) -> list[str]:
        now = utcnow_iso()
        rows = self.conn.execute(
            "SELECT host FROM host_state WHERE backing_off_until IS NOT NULL AND backing_off_until > ?",
            (now,),
        ).fetchall()
        return [r["host"] for r in rows]

    def get_host_states(self) -> list[dict]:
        """Для отчётов/GUI — состояние backoff по каждому хосту, у которого
        хоть раз была ошибка."""
        rows = self.conn.execute(
            "SELECT host, error_streak, backing_off_until, consecutive_successes "
            "FROM host_state ORDER BY host"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- статистика для UI / отчетов ---------------------------------------

    def get_counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["cnt"] for r in rows}

        rows_type = self.conn.execute(
            "SELECT type, status, COUNT(*) as cnt FROM tasks GROUP BY type, status"
        ).fetchall()
        by_type_status: dict[str, dict[str, int]] = {}
        for r in rows_type:
            by_type_status.setdefault(r["type"], {})[r["status"]] = r["cnt"]

        return {
            "by_status": by_status,
            "by_type_status": by_type_status,
            "errors_total": int(kv_get(self.conn, "stat_errors", "0")),
            "error_streak": int(kv_get(self.conn, "error_streak", "0")),
            "server_backing_off": self.is_server_backing_off(),
            "backing_off_until": self.backing_off_until(),
            "hosts_backing_off": self._currently_backing_off_hosts(),
        }


# ---------------------------------------------------------------------------
# Снимок прогресса для быстрого чтения UI (progress_state.json)
# ---------------------------------------------------------------------------

def write_progress_snapshot(path: str, counts: dict, current_task: Optional[dict] = None) -> None:
    """
    Пишет агрегированный снимок прогресса атомарно (temp-файл + os.replace),
    чтобы даже при выключении питания посреди записи файл не оказался битым.
    """
    snapshot = {
        "last_updated": utcnow_iso(),
        "current_task": current_task,
        "counts": counts,
    }
    tmp_path = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
