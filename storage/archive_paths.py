"""
archive_paths.py — построение путей к файлам внутри архива.

Вынесено в отдельный модуль, потому что и Scanner (при постановке задач в
очередь), и Downloader (при обнаружении новых страниц темы и ресурсов в
процессе скачивания) должны строить одинаковые, предсказуемые пути — см.
структуру архива в ТЗ (Archive/forum/sections/<раздел>/topics/<тема>/page_NNN.html,
Archive/images/, Archive/attachments/, Archive/avatars/, Archive/smilies/, Archive/css/).
"""

from __future__ import annotations

import hashlib
import os
from urllib.parse import urlparse, parse_qs

RESOURCE_FOLDERS = {
    "image": "images",
    "attachment": "attachments",
    "avatar": "avatars",
    "smiley": "smilies",
    "css": "css",
}


def safe_folder_name(name: str, max_len: int = 80) -> str:
    """Превращает название раздела/темы в безопасное имя папки
    (убирает символы, запрещенные в именах файлов Windows)."""
    forbidden = '/\\:*?"<>|'
    cleaned = "".join(c if c not in forbidden else "_" for c in name).strip()
    cleaned = cleaned or "unnamed"
    return cleaned[:max_len]


def section_local_path(section_path: list[str], page_index: int) -> str:
    """Путь к странице списка тем раздела (относительно корня архива)."""
    safe_parts = [safe_folder_name(p) for p in section_path]
    return "/".join(["forum", "sections", *safe_parts, f"page_{page_index:03d}.html"])


def topic_local_path(section_path: list[str], topic_name: str, page_index: int) -> str:
    """Путь к странице темы (относительно корня архива)."""
    safe_parts = [safe_folder_name(p) for p in section_path]
    safe_topic = safe_folder_name(topic_name)
    return "/".join(["forum", "sections", *safe_parts, "topics", safe_topic, f"page_{page_index:03d}.html"])


def resource_local_path(resource_type: str, url: str) -> str:
    """
    Путь к скачанному ресурсу (картинка/вложение/аватар/смайлик/CSS),
    относительно корня архива — например 'images/a1b2c3d4_photo.jpg'.

    Имя файла берется из URL, если это выглядит как разумное имя файла с
    расширением; иначе (например download/file.php?id=42, где имени файла
    в URL нет) используется короткий хэш URL как имя. Во всех случаях к
    имени добавляется префикс из хэша URL — это дешевый способ избежать
    коллизий, когда разные разделы форума случайно используют одинаковое
    имя файла (например IMG_0001.jpg из двух разных тем).
    """
    folder = RESOURCE_FOLDERS.get(resource_type, "images")
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]

    parsed = urlparse(url)
    basename = os.path.basename(parsed.path)

    if basename and "." in basename and not basename.lower().endswith(".php"):
        safe_name = safe_folder_name(basename, max_len=100)
        filename = f"{url_hash}_{safe_name}"
    else:
        # Нет полезного имени файла в пути (типично для
        # download/file.php?id=42 — "file.php" это название скрипта, а
        # не файла) — пробуем взять числовой id из query, иначе просто
        # используем хэш.
        qs = parse_qs(parsed.query)
        id_value = next((v[0] for k, v in qs.items() if k in ("id", "avatar") and v), None)
        filename = f"{url_hash}_{id_value}" if id_value else url_hash

    return f"{folder}/{filename}"
