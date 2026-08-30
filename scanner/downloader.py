"""
downloader.py — основной цикл скачивания.

Берет задачи из очереди (QueueManager), скачивает по одной, сохраняет HTML
на диск, и для страниц тем — инкрементально обнаруживает следующую страницу
(вместо того чтобы заранее сканировать всю пагинацию темы отдельным
проходом, что потребовало бы лишних запросов).

Как и Scanner: строго последовательно, один запрос за раз, throttle_delay()
после каждого успешного запроса. Вся логика backoff/retry уже в
QueueManager — Downloader только классифицирует результат fetch() и
сообщает об этом очереди.

Сохранение файлов: для каждой ТЕКСТОВОЙ страницы (раздел/тема) пишутся ДВЕ
копии — оригинал в metadata/raw_html/<тот же относительный путь> и
"чистовик" в forum/sections/.../<тот же относительный путь>. На этом этапе
чистовик пока не переписан (ссылки на картинки/вложения ведут на исходный
сервер) — переписывание делает отдельный проход rewrite_html.py после того,
как нужные ресурсы скачаны.

После сохранения каждой текстовой страницы Downloader сразу парсит её на
предмет ссылок на изображения/вложения/аватары/смайлики/CSS и добавляет их
в очередь (приоритет 2 — "ресурсы"). Сами эти задачи скачиваются, только
когда режим форума установлен в 2 ("Полный архив") — в режиме 1 они просто
лежат в очереди как pending, ничего не тратя, и готовы к обработке, как
только пользователь переключит режим.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from storage.archive_paths import topic_local_path, resource_local_path
from scanner.css_links import extract_css_links
from utils.config import AppConfig
from utils.credentials import ForumCredentials
from storage.database import (
    QueueManager, Task, get_topic, get_section_path, increment_topic_page_count,
    write_progress_snapshot, TYPE_SECTION_PAGE, TYPE_TOPIC_PAGE,
    TYPE_IMAGE, TYPE_ATTACHMENT, TYPE_AVATAR, TYPE_SMILEY, TYPE_CSS,
    STATUS_DONE,
)
from network.http_client import (
    ForumClient, FetchResult, BinaryFetchResult,
    STATUS_SUCCESS, STATUS_MISSING, STATUS_SERVER_ERROR, STATUS_AUTH_REQUIRED,
)
import parsers.phpbb_parser as _default_parser
from utils.logger_setup import get_logger

logger = get_logger()

TEXT_TASK_TYPES = {TYPE_SECTION_PAGE, TYPE_TOPIC_PAGE}
RESOURCE_TASK_TYPES = {TYPE_IMAGE, TYPE_ATTACHMENT, TYPE_AVATAR, TYPE_SMILEY, TYPE_CSS}
HANDLED_TASK_TYPES = TEXT_TASK_TYPES | RESOURCE_TASK_TYPES
BACKOFF_WAIT_CHUNK_SECONDS = 30


@dataclass
class DownloadStats:
    pages_saved: int = 0
    pages_failed: int = 0
    pages_skipped_missing: int = 0
    new_topic_pages_discovered: int = 0
    resources_saved: int = 0
    resources_failed: int = 0
    resources_skipped_missing: int = 0
    resources_discovered: int = 0
    auth_retries: int = 0
    stopped_reason: Optional[str] = None
    resources_deferred_by_mode: int = 0    # <-- ДОБАВИТЬ ЭТУ СТРОКУ


def _long_path(path: str) -> str:
    """
    На Windows полный путь длиннее ~260 символов (MAX_PATH) вызывает
    FileNotFoundError у os.makedirs/open, даже если каждое отдельное имя
    папки в пути короткое — на форуме с глубокой вложенностью
    (раздел/подраздел/тема) суммарная длина легко превышает лимит.
    Префикс \\\\?\\ переключает Win32 API на путь без этого ограничения
    (до ~32000 символов). Требует АБСОЛЮТНОГО пути с обратными слэшами —
    относительные пути и \\\\?\\ несовместимы. На других ОС не действует
    и не нужен, поэтому применяется только под os.name == "nt".
    """
    if os.name != "nt":
        return path
    abs_path = os.path.abspath(path)
    if abs_path.startswith("\\\\?\\"):
        return abs_path
    return "\\\\?\\" + abs_path


def save_html_atomic(path: str, content: str) -> None:
    """Атомарная запись текстового файла: временный файл + os.replace —
    тот же принцип надежности, что и для progress_state.json/config.json,
    чтобы даже при сбое посреди записи на диске не оставалось битых файлов."""
    long_path = _long_path(path)
    os.makedirs(os.path.dirname(long_path) or ".", exist_ok=True)
    tmp_path = long_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, long_path)


def save_bytes_atomic(path: str, content: bytes) -> None:
    """То же самое, но для бинарных файлов (картинки, вложения, CSS)."""
    long_path = _long_path(path)
    os.makedirs(os.path.dirname(long_path) or ".", exist_ok=True)
    tmp_path = long_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(content)
    os.replace(tmp_path, long_path)


class Downloader:
    def __init__(self, config: AppConfig, conn, queue_manager: QueueManager,
                 client: ForumClient, credentials: Optional[ForumCredentials] = None,
                 parser=None):
        self.config = config
        self.conn = conn
        self.qm = queue_manager
        self.client = client
        self.credentials = credentials  # для повторного входа при auth_required
        self.stats = DownloadStats()
        self.parser = parser or _default_parser

    def run(self, max_tasks: Optional[int] = None, wait_for_backoff: bool = True,
            allowed_section_ids: Optional[list[str]] = None) -> DownloadStats:
        """
        Основной цикл. Останавливается, когда:
            - очередь пуста для текущего режима (mode 1/2);
            - достигнут max_tasks (используется для тестов/ограниченных прогонов);
            - сервер в backoff и wait_for_backoff=False (используется в
              тестах и для интерактивных прогонов, где не хочется ждать
              часами — процесс можно просто перезапустить позже, прогресс
              уже сохранен).

        allowed_section_ids — выборочная архивация: если задан список id
        разделов, скачиваются только задачи этих разделов (и ресурсы с их
        страниц) — остальное остаётся в очереди нетронутым для будущего
        прогона. None (по умолчанию) — скачивается всё, как раньше.
        """
        processed = 0
        while True:
            if max_tasks is not None and processed >= max_tasks:
                self.stats.stopped_reason = "Достигнут лимит max_tasks"
                break

            task = self.qm.get_next_task(priority_ceiling=self.config.mode.priority_ceiling(),
                                          allowed_section_ids=allowed_section_ids)

            if task is None:
                if self.qm.is_server_backing_off():
                    if not wait_for_backoff:
                        self.stats.stopped_reason = (
                            f"Сервер в состоянии backoff до {self.qm.backing_off_until()} — "
                            f"остановлено (wait_for_backoff=False). Прогресс сохранен, "
                            f"можно перезапустить позже."
                        )
                        break
                    self._wait_for_backoff()
                    continue

                host_wait = self.qm.host_backoff_wait_seconds(
                    priority_ceiling=self.config.mode.priority_ceiling(),
                    allowed_section_ids=allowed_section_ids,
                )
                if host_wait is not None:
                    # Задачи есть, но принадлежат хосту (хостам), которые
                    # сейчас "отдыхают" — это НЕ то же самое, что пустая
                    # очередь. Остальные хосты (включая сам форум) при этом
                    # не заблокированы — просто в этот конкретный момент
                    # свободных задач с других хостов не нашлось.
                    if not wait_for_backoff:
                        self.stats.stopped_reason = (
                            f"Часть задач ждёт окончания backoff по отдельному хосту "
                            f"(~{host_wait:.0f} сек.) — остановлено (wait_for_backoff=False). "
                            f"Прогресс сохранен, можно перезапустить позже."
                        )
                        break
                    time.sleep(min(host_wait, BACKOFF_WAIT_CHUNK_SECONDS) + 0.1)
                    continue

                self.stats.stopped_reason = "Очередь пуста — скачивать больше нечего"
                break

            self._process_task(task)
            processed += 1

            write_progress_snapshot(
                self.config.paths.progress_json_path(),
                self.qm.get_counts(),
                current_task={"type": task.type, "url": task.url},
            )
            self.stats.resources_deferred_by_mode = self.qm.count_deferred_by_mode(
            self.config.mode.priority_ceiling()
        )
        return self.stats

    def backfill_css_imports(self) -> int:
        """
        Разовая (но безопасно повторяемая) доначинка для CSS-файлов,
        скачанных ДО того, как Downloader научился разбирать @import
        внутри них (см. _enqueue_css_imports) — например, архив,
        начатый до обновления программы. Не делает сетевых запросов сама:
        читает уже скачанные CSS-файлы с диска, ищет в них @import/url(...)
        и доставляет найденные ссылки в очередь тем же способом, каким
        это происходит автоматически при свежем скачивании CSS — сами
        файлы скачает следующий обычный запуск run().

        Возвращает число вновь поставленных в очередь задач. Безопасно
        запускать сколько угодно раз — add_task идемпотентен (уже
        известный URL повторно не добавляется), поэтому повторный вызов
        просто ничего не найдёт нового. Число новых задач считается по
        реальному числу строк в tasks (не по self.stats.resources_discovered
        — тот счётчик, как и в _enqueue_resources, растёт при каждом
        вызове add_task независимо от того, была ли задача новой или уже
        существовала, что для однократного вызова на свежей странице не
        важно, но ломало бы идемпотентность именно этого метода).
        """
        archive_root = self.config.paths.archive_root
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = ? AND type = ?",
            (STATUS_DONE, TYPE_CSS),
        ).fetchall()

        count_before = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

        for row in rows:
            task = Task.from_row(row)
            full_path = os.path.join(archive_root, task.local_path)
            if not os.path.exists(full_path):
                continue
            with open(full_path, "rb") as f:
                css_content = f.read()
            self._enqueue_css_imports(task, css_content)

        count_after = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        return count_after - count_before

    def _wait_for_backoff(self) -> None:
        """Ждет окончания backoff чанками, а не одним долгим sleep() —
        чтобы не блокировать процесс на часы без возможности прерваться
        и чтобы периодически логировать, что происходит."""
        until_str = self.qm.backing_off_until()
        if not until_str:
            return
        until = datetime.fromisoformat(until_str)
        while True:
            now = datetime.now(until.tzinfo)
            remaining = (until - now).total_seconds()
            if remaining <= 0:
                return
            wait_time = min(remaining, BACKOFF_WAIT_CHUNK_SECONDS)
            logger.info(f"Сервер в состоянии backoff, жду еще {remaining:.0f} сек. (до {until_str})")
            time.sleep(wait_time)

    def _process_task(self, task) -> None:
        if task.type in TEXT_TASK_TYPES:
            self._process_text_task(task)
        elif task.type in RESOURCE_TASK_TYPES:
            self._process_resource_task(task)
        else:
            logger.error(f"Тип задачи '{task.type}' не поддерживается Downloader.")
            self.qm.mark_task_failed(task.id, f"Тип задачи '{task.type}' не реализован")
            self.stats.pages_failed += 1

    def _process_text_task(self, task) -> None:
        result = self.client.fetch(task.url)

        if result.status == STATUS_AUTH_REQUIRED:
            result = self._retry_with_login(task)

        if result.status == STATUS_SUCCESS:
            self._save_and_followup(task, result.text)
            self.qm.mark_task_done(task.id)
            self.stats.pages_saved += 1
            self.client.throttle_delay()

        elif result.status == STATUS_MISSING:
            self.qm.mark_task_skipped_missing(task.id, result.error_message or "404/410")
            self.stats.pages_skipped_missing += 1

        elif result.status == STATUS_SERVER_ERROR:
            self.qm.mark_task_failed(task.id, result.error_message or "Ошибка сервера")
            self.stats.pages_failed += 1

        else:  # STATUS_AUTH_REQUIRED (после неудачной повторной попытки логина) / STATUS_CLIENT_ERROR
            self.qm.mark_task_failed(task.id, result.error_message or f"Статус: {result.status}")
            self.stats.pages_failed += 1

    def _process_resource_task(self, task) -> None:
        result = self.client.fetch_binary(task.url)

        if result.status == STATUS_AUTH_REQUIRED:
            result = self._retry_with_login_binary(task)

        if result.status == STATUS_SUCCESS:
            final_path = os.path.join(self.config.paths.archive_root, task.local_path)
            save_bytes_atomic(final_path, result.content)
            self.qm.mark_task_done(task.id)
            self.stats.resources_saved += 1

            if task.type == TYPE_CSS:
                self._enqueue_css_imports(task, result.content)

            self.client.throttle_delay()

        elif result.status == STATUS_MISSING:
            self.qm.mark_task_skipped_missing(task.id, result.error_message or "404/410")
            self.stats.resources_skipped_missing += 1

        elif result.status == STATUS_SERVER_ERROR:
            self.qm.mark_task_failed(task.id, result.error_message or "Ошибка сервера")
            self.stats.resources_failed += 1

        else:  # STATUS_AUTH_REQUIRED (после неудачной попытки) / STATUS_CLIENT_ERROR
            self.qm.mark_task_failed(task.id, result.error_message or f"Статус: {result.status}")
            self.stats.resources_failed += 1

    def _login(self) -> bool:
        """
        Вызывает нужный метод входа у client — какой именно, определяется
        именем метода в parser.LOGIN_METHOD_NAME (по умолчанию "login" —
        обычный вход phpBB). Тот же принцип, что и с HOME_PAGE_PATH в
        scanner.py — сменная часть определяется модулем-парсером, а не
        хардкодится тут, чтобы Downloader не нужно было трогать при
        добавлении следующего форума/движка.
        """
        method_name = getattr(self.parser, "LOGIN_METHOD_NAME", "login")
        login_method = getattr(self.client, method_name)
        return login_method(self.credentials.username, self.credentials.password)

    def _retry_with_login(self, task) -> FetchResult:
        """Сессия истекла посреди работы — одна попытка перелогиниться и
        повторить запрос, прежде чем сдаваться."""
        if not self.credentials or not self.credentials.is_set():
            return FetchResult(status=STATUS_AUTH_REQUIRED,
                                error_message="Требуется вход, но учетные данные не заданы (.env пуст)")

        logger.warning(f"Сессия истекла при запросе {task.url} — повторный вход...")
        self.stats.auth_retries += 1
        try:
            ok = self._login()
        except Exception as e:
            return FetchResult(status=STATUS_AUTH_REQUIRED, error_message=f"Ошибка повторного входа: {e}")

        if not ok:
            return FetchResult(status=STATUS_AUTH_REQUIRED, error_message="Повторный вход не удался")

        return self.client.fetch(task.url)

    def _retry_with_login_binary(self, task) -> BinaryFetchResult:
        """То же самое, что _retry_with_login, но для бинарных ресурсов."""
        if not self.credentials or not self.credentials.is_set():
            return BinaryFetchResult(status=STATUS_AUTH_REQUIRED,
                                      error_message="Требуется вход, но учетные данные не заданы (.env пуст)")

        logger.warning(f"Сессия истекла при запросе {task.url} — повторный вход...")
        self.stats.auth_retries += 1
        try:
            ok = self._login()
        except Exception as e:
            return BinaryFetchResult(status=STATUS_AUTH_REQUIRED, error_message=f"Ошибка повторного входа: {e}")

        if not ok:
            return BinaryFetchResult(status=STATUS_AUTH_REQUIRED, error_message="Повторный вход не удался")

        return self.client.fetch_binary(task.url)

    def _save_and_followup(self, task, html: str) -> None:
        archive_root = self.config.paths.archive_root
        final_path = os.path.join(archive_root, task.local_path)
        raw_path = os.path.join(archive_root, "metadata", "raw_html", task.local_path)

        save_html_atomic(raw_path, html)
        save_html_atomic(final_path, html)

        if task.type == TYPE_TOPIC_PAGE and task.parent_id:
            self._discover_next_topic_page(task, html)

        self._enqueue_resources(task, html)

    def _enqueue_resources(self, task, html: str) -> None:
        """
        Парсит только что сохраненную страницу на предмет ссылок на
        изображения/вложения/аватары/смайлики/CSS и добавляет их в очередь
        (приоритет 2 — "ресурсы"). Это не стоит лишних запросов к серверу
        (парсинг уже скачанного HTML), поэтому делается всегда, независимо
        от текущего режима (1/2) — задачи просто будут ждать в очереди
        pending, пока пользователь не переключится на режим "Полный архив".
        """
        base_url = self.config.forum.base_url
        links = self.parser.extract_resource_links(html, base_url)
        for link in links:
            local_path = resource_local_path(link.type, link.url)
            new_task_id = self.qm.add_task(link.type, link.url, local_path, parent_id=task.id)
            if new_task_id:
                self.stats.resources_discovered += 1

    def _enqueue_css_imports(self, task, css_content: bytes) -> None:
        """
        Парсит только что скачанный CSS-файл на предмет @import url(...) /
        url(...) на другие CSS-файлы (или картинки/шрифты) и добавляет их
        в очередь как задачи типа css, с parent_id=task.id — тот же
        механизм наследования section_id для выборочной архивации, что и
        у обычных ресурсов со страницы (см. _enqueue_resources).

        Нужно отдельно от _enqueue_resources, потому что типичный phpBB
        style.css сам почти пустой и подключает 10+ других файлов
        (normalize.css, base.css, common.css, colours.css, ...) через
        @import — без этого шага они никогда не попадают в очередь на
        скачивание, и итоговое оформление архива остаётся неполным даже
        когда сам style.css успешно скачан.
        """
        try:
            css_text = css_content.decode("utf-8", errors="replace")
        except Exception:
            return

        links = extract_css_links(css_text, task.url)
        for link in links:
            # url(...) внутри CSS может указывать не только на другой CSS
            # (@import), но и на картинку/шрифт (фон темы, @font-face) —
            # кладём такие в images, а не в css, по расширению в самой
            # ссылке (без query-параметров вроде "?v=3.2").
            path_only = link.full_url.split("?", 1)[0].split("#", 1)[0]
            resource_type = TYPE_CSS if path_only.lower().endswith(".css") else TYPE_IMAGE
            local_path = resource_local_path(resource_type, link.full_url)
            new_task_id = self.qm.add_task(resource_type, link.full_url, local_path, parent_id=task.id)
            if new_task_id:
                self.stats.resources_discovered += 1

    def _discover_next_topic_page(self, task, html: str) -> None:
        next_url = self.parser.find_next_page_url(html, task.url)
        if not next_url:
            return

        topic_id = task.parent_id
        topic_row = get_topic(self.conn, topic_id)
        if topic_row is None:
            logger.warning(
                f"Тема {topic_id} не найдена в таблице topics — "
                f"пропускаю поиск следующей страницы для {task.url}"
            )
            return

        section_path = get_section_path(self.conn, topic_row["section_id"])
        new_page_index = increment_topic_page_count(self.conn, topic_id)
        next_local_path = topic_local_path(section_path, topic_row["name"], new_page_index)

        new_task_id = self.qm.add_task(TYPE_TOPIC_PAGE, next_url, next_local_path,
                                        parent_id=topic_id, section_id=topic_row["section_id"])
        if new_task_id:
            self.stats.new_topic_pages_discovered += 1
            logger.info(
                f"Обнаружена следующая страница темы '{topic_row['name']}' "
                f"(стр. {new_page_index}): {next_url}"
            )
