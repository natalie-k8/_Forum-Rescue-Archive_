"""
rewrite_html.py — переписывание сохраненных HTML-страниц: замена ссылок
на изображения/вложения/аватары/смайлики/CSS на локальные относительные
пути, чтобы архив можно было открыть и просматривать полностью автономно,
без интернета (см. п.9 ТЗ "Работа без интернета").

Работает как ОТДЕЛЬНЫЙ, повторяемый проход поверх уже скачанных данных:
    - читает "сырой" оригинал из metadata/raw_html/<путь>;
    - для каждого ресурса, ссылка на который встречается в HTML, смотрит,
      скачан ли он (таблица tasks, status='done') — если да, заменяет
      src/href на относительный путь до локального файла;
    - результат записывает поверх "чистовика" в forum/sections/<путь>.

Работа с оригинала, а не с уже переписанного чистовика, делает проход
идемпотентным: можно запускать сколько угодно раз по мере того, как
докачиваются новые ресурсы, не боясь испортить уже переписанные ссылки.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urljoin
from typing import Optional

from bs4 import BeautifulSoup

from utils.config import AppConfig
from storage.database import TYPE_SECTION_PAGE, TYPE_TOPIC_PAGE, STATUS_DONE
from scanner.downloader import save_html_atomic
from utils.logger_setup import get_logger

logger = get_logger()


@dataclass
class RewriteStats:
    pages_rewritten: int = 0
    links_replaced: int = 0
    pages_skipped_no_raw: int = 0


def _build_resource_url_map(conn, base_url: str) -> dict[str, str]:
    """
    Строит словарь {абсолютный URL ресурса -> локальный путь от корня
    архива} по всем успешно скачанным ресурсам (status='done', тип не
    текстовая страница). Используется для замены ссылок при переписывании.
    """
    rows = conn.execute(
        "SELECT url, local_path FROM tasks "
        "WHERE status = ? AND type NOT IN (?, ?)",
        (STATUS_DONE, TYPE_SECTION_PAGE, TYPE_TOPIC_PAGE),
    ).fetchall()
    return {row["url"]: row["local_path"] for row in rows}


def _relative_path(from_local_path: str, to_local_path: str) -> str:
    """
    Относительный путь от файла-страницы (from_local_path, например
    'forum/sections/A/topics/B/page_001.html') до ресурса (to_local_path,
    например 'images/xxxx.jpg') — оба заданы относительно корня архива.
    """
    from_dir = os.path.dirname(from_local_path)
    rel = os.path.relpath(to_local_path, start=from_dir)
    return rel.replace(os.sep, "/")


def rewrite_page(html: str, base_url: str, page_local_path: str,
                  resource_map: dict[str, str]) -> tuple[str, int]:
    """
    Переписывает один HTML-документ: заменяет src/href ресурсов, которые
    есть в resource_map, на локальные относительные пути. Возвращает
    (новый_html, количество_замен).
    """
    soup = BeautifulSoup(html, "lxml")
    replaced = 0

    def resolve(raw_url: str) -> Optional[str]:
        full_url = urljoin(base_url, raw_url.replace("&amp;", "&"))
        return resource_map.get(full_url)

    for img in soup.find_all("img", src=True):
        local = resolve(img["src"])
        if local:
            img["src"] = _relative_path(page_local_path, local)
            replaced += 1

    for a in soup.find_all("a", href=True):
        local = resolve(a["href"])
        if local:
            a["href"] = _relative_path(page_local_path, local)
            replaced += 1

    for link in soup.find_all("link", rel="stylesheet", href=True):
        local = resolve(link["href"])
        if local:
            link["href"] = _relative_path(page_local_path, local)
            replaced += 1

    return str(soup), replaced


def rewrite_all_pages(config: AppConfig, conn) -> RewriteStats:
    """
    Проходит по всем успешно скачанным текстовым страницам (section_page,
    topic_page) и переписывает их чистовые копии, заменяя ссылки на уже
    скачанные ресурсы локальными путями. Безопасно запускать повторно —
    каждый раз читает из оригинала (metadata/raw_html), а не поверх
    предыдущего результата.
    """
    stats = RewriteStats()
    base_url = config.forum.base_url
    archive_root = config.paths.archive_root

    resource_map = _build_resource_url_map(conn, base_url)
    logger.info(f"Переписывание HTML: найдено {len(resource_map)} скачанных ресурсов для подстановки")

    rows = conn.execute(
        "SELECT local_path FROM tasks WHERE status = ? AND type IN (?, ?)",
        (STATUS_DONE, TYPE_SECTION_PAGE, TYPE_TOPIC_PAGE),
    ).fetchall()

    for row in rows:
        local_path = row["local_path"]
        raw_path = os.path.join(archive_root, "metadata", "raw_html", local_path)
        final_path = os.path.join(archive_root, local_path)

        if not os.path.exists(raw_path):
            stats.pages_skipped_no_raw += 1
            logger.warning(f"Оригинал не найден для {local_path} — пропущено")
            continue

        with open(raw_path, "r", encoding="utf-8") as f:
            raw_html = f.read()

        new_html, replaced = rewrite_page(raw_html, base_url, local_path, resource_map)
        save_html_atomic(final_path, new_html)

        stats.pages_rewritten += 1
        stats.links_replaced += replaced

    logger.info(
        f"Переписывание завершено: {stats.pages_rewritten} страниц, "
        f"{stats.links_replaced} ссылок заменено на локальные"
    )
    return stats
