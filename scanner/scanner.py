"""
scanner.py — обход структуры форума (разделы -> подразделы -> темы) и
постановка задач на скачивание в очередь (QueueManager).

Работает как "очень осторожный пользователь": каждый запрос — через
ForumClient.fetch(), после КАЖДОГО успешного запроса — throttle_delay()
(п.1 ТЗ). Ошибки сервера не поднимаются исключением — Scanner сам решает,
что делать: сохранить прогресс скана и остановиться, залогировав причину;
пользователь (или планировщик заданий) запускает Scanner повторно позже,
и он продолжит с того места, где остановился (см. incremental-скан ниже).

Инкрементальность скана обеспечивается полем `scanned` в таблицах
sections/topics: раздел или тема, которые уже отсканированы, повторно не
обрабатываются. Это позволяет прервать и перезапустить Scanner в любой
момент без потери уже собранной структуры.

Обнаружение всех страниц темы (пагинация) НЕ выполняется на этапе Scanner —
достаточно поставить в очередь только первую страницу темы. Вторая и
последующие страницы обнаруживаются позже, во время самого скачивания
(Downloader), через find_next_page_url() из phpbb_parser.py — это избавляет
от лишнего запроса "просканировать тему, чтобы узнать число страниц, а
потом еще раз ее скачать".
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urljoin
from typing import Optional

from storage.archive_paths import section_local_path as _section_local_path
from storage.archive_paths import topic_local_path as _topic_local_path
from utils.config import AppConfig
from storage.database import (
    QueueManager, TYPE_SECTION_PAGE, TYPE_TOPIC_PAGE, utcnow_iso, get_section_path,
)
from network.http_client import ForumClient, STATUS_SUCCESS, STATUS_AUTH_REQUIRED, STATUS_SERVER_ERROR, STATUS_MISSING
import parsers.phpbb_parser as _default_parser
from utils.logger_setup import get_logger

logger = get_logger()

# Scanner делает "разведывательные" запросы к серверу напрямую (не через
# очередь), поэтому у него нет доступа к полноценному backoff из
# QueueManager. Транзитные сбои (сброс соединения, кратковременная
# перегрузка) не должны требовать от пользователя вручную перезапускать
# весь скрипт — поэтому здесь используется свой отдельный, более легкий
# набор повторных попыток перед тем как сдаться и сохранить прогресс.
SCAN_RETRY_DELAYS_SECONDS = [15, 60, 180]


@dataclass
class ScanStats:
    sections_discovered: int = 0
    topics_discovered: int = 0
    section_pages_enqueued: int = 0
    topic_pages_enqueued: int = 0
    stopped_reason: Optional[str] = None


class Scanner:
    def __init__(self, config: AppConfig, conn: sqlite3.Connection,
                 queue_manager: QueueManager, client: ForumClient, parser=None):
        self.config = config
        self.conn = conn
        self.qm = queue_manager
        self.client = client
        self.stats = ScanStats()
        self.test_mode = config.test_mode
        # По умолчанию — phpbb_parser (как и раньше). Передав forum2x2_parser
        # (или любой другой модуль с той же сигнатурой функций), Scanner
        # будет работать с другим движком форума без изменения своего кода.
        self.parser = parser or _default_parser

    # -- запрос с легкими повторными попытками ------------------------------

    def _fetch_with_retry(self, url: str):
        """
        Как и Downloader, Scanner должен переживать единичные транзитные
        сбои (сброс соединения, кратковременная перегрузка сервера), не
        требуя от пользователя вручную перезапускать весь скрипт из-за
        каждого мимолетного обрыва. Повторяет запрос только при
        STATUS_SERVER_ERROR; 404/успех/auth_required возвращаются сразу,
        без повторов — это не транзитные ошибки.
        """
        last_result = None
        for attempt, delay in enumerate([0] + SCAN_RETRY_DELAYS_SECONDS):
            if delay:
                logger.warning(
                    f"Повторная попытка запроса {url} через {delay} сек. "
                    f"(попытка {attempt} из-за: {last_result.error_message if last_result else ''})"
                )
                time.sleep(delay)
            result = self.client.fetch(url)
            if result.status != STATUS_SERVER_ERROR:
                return result
            last_result = result
        return last_result

    # -- вспомогательные операции с БД структуры ---------------------------

    def _section_exists(self, section_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sections WHERE id = ?", (section_id,)
        ).fetchone() is not None

    def _insert_section(self, section_id: str, forum_id: str, name: str, url: str,
                         parent_id: Optional[str]) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO sections (id, forum_id, name, url, parent_id, page_count, scanned)
            VALUES (?, ?, ?, ?, ?, 1, 0)
            """,
            (section_id, forum_id, name, url, parent_id),
        )
        self.conn.commit()

    def _mark_section_scanned(self, section_id: str) -> None:
        self.conn.execute("UPDATE sections SET scanned = 1 WHERE id = ?", (section_id,))
        self.conn.commit()

    def _unscanned_sections(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sections WHERE scanned = 0 ORDER BY rowid ASC"
        ).fetchall()

    def _section_path(self, section_id: str) -> list[str]:
        """Строит путь имен от корня до раздела (для структуры папок на диске)."""
        return get_section_path(self.conn, section_id)

    def _insert_topic(self, topic_id: str, section_id: str, name: str, url: str,
                       author: Optional[str]) -> bool:
        """Возвращает True, если тема была новой (не существовала ранее)."""
        existing = self.conn.execute(
            "SELECT 1 FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()
        if existing:
            return False
        self.conn.execute(
            """
            INSERT INTO topics (id, section_id, name, url, author, page_count, scanned)
            VALUES (?, ?, ?, ?, ?, 1, 1)
            """,
            (topic_id, section_id, name, url, author),
        )
        self.conn.commit()
        return True

    # -- основной цикл -------------------------------------------------------

    def run(self) -> ScanStats:
        """
        Запускает (или продолжает) скан. Останавливается сам при:
            - достижении лимитов test_mode;
            - ошибке сервера (после N ошибок подряд backoff остановит выдачу
              новых запросов — Scanner в этом случае просто завершает текущий
              проход, прогресс уже сохранен в БД построчно);
            - истечении сессии, если требуется логин, но он не выполнен.

        Безопасно вызывать повторно — продолжит с несканированных разделов.
        """
        base_url = self.config.forum.base_url
        if not base_url:
            raise ValueError("config.forum.base_url не задан — укажите адрес форума перед сканированием")

        if not self._bootstrap_root_sections(base_url):
            return self.stats

        while True:
            if self.test_mode.enabled and self.test_mode.max_total_tasks is not None \
                    and (self.stats.section_pages_enqueued + self.stats.topic_pages_enqueued) \
                    >= self.test_mode.max_total_tasks:
                self.stats.stopped_reason = "Достигнут лимит test_mode.max_total_tasks"
                logger.info(self.stats.stopped_reason)
                break

            unscanned = self._unscanned_sections()
            if not unscanned:
                self.stats.stopped_reason = "Все известные разделы отсканированы"
                logger.info(self.stats.stopped_reason)
                break

            section_row = unscanned[0]
            ok = self._scan_section(section_row, base_url)
            if not ok:
                # Причина остановки уже записана в self.stats.stopped_reason
                break

        return self.stats

    def _bootstrap_root_sections(self, base_url: str) -> bool:
        """
        Сканирует index.php, чтобы получить разделы верхнего уровня.

        Запрашивается КАЖДЫЙ раз (не только при самом первом запуске) —
        иначе, если ранний прогон нашёл лишь часть разделов верхнего
        уровня (например, тестовый режим с лимитом max_sections), новые
        разделы, добавленные на форуме позже или просто не найденные
        тогда, никогда бы не обнаружились при последующих безлимитных
        запусках, сколько бы раз программу ни перезапускали. Уже
        известные разделы не трогаются: _insert_section использует
        INSERT OR IGNORE, так что их scanned-статус не сбрасывается.
        """
        # index.php — только у phpBB; у forum2x2 (и, вероятно, у любого
        # другого движка) домашняя страница — это сам base_url. Поэтому
        # путь берётся из самого модуля-парсера, а не хардкодится тут.
        home_path = getattr(self.parser, "HOME_PAGE_PATH", "index.php")
        index_url = urljoin(base_url, home_path)
        result = self._fetch_with_retry(index_url)

        if result.status == STATUS_AUTH_REQUIRED:
            self.stats.stopped_reason = (
                f"Требуется вход для доступа к главной странице форума ({index_url}). "
                "Проверьте учетные данные и повторите запуск."
            )
            logger.error(self.stats.stopped_reason)
            return False

        if result.status != STATUS_SUCCESS:
            self.stats.stopped_reason = f"Не удалось загрузить главную страницу форума ({index_url}): {result.error_message}"
            logger.error(self.stats.stopped_reason)
            return False

        self.client.throttle_delay()

        forums = self.parser.extract_forum_links(result.text, base_url)
        for f in forums:
            if self.test_mode.enabled and self.test_mode.max_sections is not None \
                    and self.stats.sections_discovered >= self.test_mode.max_sections:
                break
            already_known = self._section_exists(f.forum_id)
            self._insert_section(section_id=f.forum_id, forum_id=f.forum_id,
                                  name=f.name, url=f.url, parent_id=None)
            if not already_known:
                self.stats.sections_discovered += 1

        logger.info(f"Обнаружено {self.stats.sections_discovered} новых раздела(ов) верхнего уровня")
        return True

    def _scan_section(self, section_row: sqlite3.Row, base_url: str) -> bool:
        """
        Обрабатывает один раздел: проходит по всем страницам его списка тем,
        собирает подфорумы и темы, ставит section_page задачи на скачивание.
        Возвращает False, если нужно прервать весь скан (ошибка сервера/логина).
        """
        section_id = section_row["id"]
        section_url = section_row["url"]
        section_path = self._section_path(section_id)

        first_page_result = self._fetch_with_retry(section_url)
        if first_page_result.status == STATUS_AUTH_REQUIRED:
            self.stats.stopped_reason = (
                f"Раздел '{section_row['name']}' требует входа, к которому нет доступа "
                f"с текущими учетными данными. Раздел пропущен, скан продолжится с других разделов."
            )
            logger.warning(self.stats.stopped_reason)
            self._mark_section_scanned(section_id)  # чтобы не зацикливаться на нем
            self.stats.stopped_reason = None
            return True
        if first_page_result.status == STATUS_MISSING:
            logger.warning(f"Раздел '{section_row['name']}' не найден (404) — пропущен")
            self._mark_section_scanned(section_id)
            return True
        if first_page_result.status == STATUS_SERVER_ERROR:
            self.stats.stopped_reason = f"Ошибка сервера при сканировании раздела '{section_row['name']}': {first_page_result.error_message}"
            logger.error(self.stats.stopped_reason)
            return False

        self.client.throttle_delay()

        # Подфорумы, обнаруженные на первой странице раздела
        sub_forums = self.parser.extract_forum_links(first_page_result.text, base_url)
        for sf in sub_forums:
            if sf.forum_id == section_id:
                continue
            if not self._section_exists(sf.forum_id):
                if self.test_mode.enabled and self.test_mode.max_sections is not None \
                        and self.stats.sections_discovered >= self.test_mode.max_sections:
                    continue
                self._insert_section(sf.forum_id, sf.forum_id, sf.name, sf.url, parent_id=section_id)
                self.stats.sections_discovered += 1

        # Все страницы списка тем в этом разделе
        page_urls = self.parser.discover_pagination_urls(first_page_result.text, section_url)
        pages_html = {page_urls[0]: first_page_result.text}

        for page_index, page_url in enumerate(page_urls, start=1):
            if self.test_mode.enabled and self.test_mode.max_total_tasks is not None \
                    and (self.stats.section_pages_enqueued + self.stats.topic_pages_enqueued) \
                    >= self.test_mode.max_total_tasks:
                break

            if page_url in pages_html:
                html = pages_html[page_url]
            else:
                page_result = self._fetch_with_retry(page_url)
                if page_result.status == STATUS_SERVER_ERROR:
                    self.stats.stopped_reason = (
                        f"Ошибка сервера на странице {page_index} раздела '{section_row['name']}': "
                        f"{page_result.error_message}"
                    )
                    logger.error(self.stats.stopped_reason)
                    return False
                if page_result.status != STATUS_SUCCESS:
                    logger.warning(f"Страница {page_index} раздела '{section_row['name']}' пропущена: {page_result.error_message}")
                    continue
                self.client.throttle_delay()
                html = page_result.text

            # Ставим в очередь задачу на сохранение самой страницы раздела
            local_path = _section_local_path(section_path, page_index)
            self.qm.add_task(TYPE_SECTION_PAGE, page_url, local_path, parent_id=None,
                              section_id=section_id)
            self.stats.section_pages_enqueued += 1

            # Темы на этой странице
            topics = self.parser.extract_topic_links(html, base_url)
            topic_count_this_section = self.test_mode.max_topics_per_section
            section_topic_limit_reached = False
            for topic in topics:
                if topic_count_this_section is not None and self.test_mode.enabled:
                    already = self.conn.execute(
                        "SELECT COUNT(*) as c FROM topics WHERE section_id = ?", (section_id,)
                    ).fetchone()["c"]
                    if already >= topic_count_this_section:
                        section_topic_limit_reached = True
                        break

                is_new = self._insert_topic(topic.topic_id, section_id, topic.name, topic.url, topic.author)
                if is_new:
                    self.stats.topics_discovered += 1
                    topic_local_path = _topic_local_path(section_path, topic.name, 1)
                    self.qm.add_task(TYPE_TOPIC_PAGE, topic.url, topic_local_path,
                                      parent_id=topic.topic_id, section_id=section_id)
                    self.stats.topic_pages_enqueued += 1

                    if self.test_mode.enabled and self.test_mode.max_pages_per_topic is not None:
                        # В тестовом режиме дополнительно фиксируем ограничение
                        # на число страниц темы — само ограничение применяется
                        # позже, в Downloader, при обнаружении next-страницы.
                        pass

            topics_known_in_section = self.conn.execute(
                "SELECT COUNT(*) as c FROM topics WHERE section_id = ?", (section_id,)
            ).fetchone()["c"]
            logger.info(
                f"Раздел '{section_row['name']}': страница списка тем {page_index}"
                f"{'/' + str(len(page_urls)) if len(page_urls) > 1 else ''}, "
                f"тем в разделе известно всего — {topics_known_in_section}"
            )

            if section_topic_limit_reached:
                # Лимит тем на раздел (test_mode.max_topics_per_section) уже
                # достигнут — дальнейшие страницы списка тем в этом разделе
                # смысла листать нет: новых тем мы всё равно не возьмём, а
                # каждая страница — это лишний запрос с паузой. Останавливаем
                # именно листание СПИСКА тем в этом разделе; раздел всё равно
                # помечается полностью отсканированным ниже (см. примечание
                # в конце функции), чтобы при повторном запуске сканер не
                # возвращался к нему повторно.
                logger.info(
                    f"Раздел '{section_row['name']}': лимит тем на раздел "
                    f"({topic_count_this_section}) достигнут — оставшиеся "
                    f"страницы списка тем ({len(page_urls) - page_index} из "
                    f"{len(page_urls)}) пропущены."
                )
                break

        self._mark_section_scanned(section_id)
        logger.info(
            f"Раздел '{section_row['name']}' отсканирован: "
            f"{len(page_urls)} стр. списка, найдено тем в разделе."
        )
        return True
