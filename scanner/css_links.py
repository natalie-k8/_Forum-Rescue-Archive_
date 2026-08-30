"""
css_links.py — поиск ссылок на другие ресурсы ВНУТРИ уже скачанного
CSS-файла: @import url(...) / @import "..." (подключение других
таблиц стилей) и url(...) в свойствах (фоновые картинки, шрифты).

Нужен отдельно от parsers/*_parser.py, потому что это не парсинг
HTML конкретного движка форума, а разбор CSS — тот же синтаксис
одинаков для phpBB, forum2x2 и любого другого движка. Используется и
downloader.py (чтобы поставить найденные ссылки в очередь на
скачивание), и rewrite_html.py (чтобы переписать их на локальные пути
после скачивания) — вынесено сюда, а не в один из этих двух модулей,
чтобы не создавать циклический импорт между ними.

Пример типичного phpBB style.css:
    @import url("normalize.css?v=3.2");
    @import url("colours.css?v=3.2");
Без обработки этого файла ссылки на normalize.css/colours.css/... не
попадают в очередь на скачивание вообще — сканер видит только
<link rel="stylesheet"> в HTML, а не то, что происходит "на втором
уровне" внутри самого CSS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

# @import url("...");  |  @import url(...);  |  @import "...";  |  @import '...';
CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*)?["']?([^"')\s;]+)["']?\s*\)?\s*[^;]*;""",
    re.IGNORECASE,
)
# url(...) в любом свойстве (background, @font-face src, и т.п.) — включая
# те же @import url(...), но CSS_IMPORT_RE обрабатывается первым и о
# дублировании заботится вызывающий код (одинаковые абсолютные URL).
CSS_URL_RE = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", re.IGNORECASE)

# data: URI и якоря-заглушки — не настоящие внешние ресурсы, пропускаем.
SKIP_URL_PREFIXES = ("data:", "#")


@dataclass
class CssLink:
    raw_url: str    # ссылка ровно как написана в CSS (для точной замены строкой)
    full_url: str   # абсолютный URL (для скачивания/поиска в resource_map)


def extract_css_links(css_text: str, base_css_url: str) -> list[CssLink]:
    """
    Находит все @import и url(...) ссылки в тексте CSS-файла.

    base_css_url — абсолютный URL самого CSS-файла (не форума!) — ссылки
    внутри CSS (например "normalize.css?v=3.2") разрешаются относительно
    расположения ЭТОГО файла на сервере, а не относительно домашней
    страницы форума.
    """
    seen: set[str] = set()
    results: list[CssLink] = []

    def add(raw_url: str) -> None:
        raw_url = raw_url.strip()
        if not raw_url or raw_url.startswith(SKIP_URL_PREFIXES):
            return
        full_url = urljoin(base_css_url, raw_url)
        if full_url in seen:
            return
        seen.add(full_url)
        results.append(CssLink(raw_url=raw_url, full_url=full_url))

    for match in CSS_IMPORT_RE.finditer(css_text):
        add(match.group(1))

    for match in CSS_URL_RE.finditer(css_text):
        add(match.group(1))

    return results


def rewrite_css_links(css_text: str, base_css_url: str, css_local_path: str,
                       resource_map: dict[str, str], relative_path_fn) -> tuple[str, int]:
    """
    Заменяет @import/url(...) ссылки в css_text на локальные пути, если
    ресурс есть в resource_map (абсолютный URL -> local_path от корня
    архива).

    base_css_url — абсолютный URL самого CSS-файла на сервере (нужен,
    чтобы разрешать относительные ссылки внутри него, например
    "normalize.css?v=3.2", в абсолютный URL для поиска в resource_map).
    css_local_path — путь САМОГО CSS-файла на диске, относительно корня
    архива (нужен relative_path_fn, чтобы посчитать относительный путь
    ОТ него ДО найденного ресурса — та же функция, что используется для
    HTML-страниц).

    Возвращает (новый_текст, количество_замен). Работает строковой
    заменой по raw_url, найденному в самом CSS-тексте — не трогает
    остальной текст файла.
    """
    replaced = 0

    def resolve_and_replace(match: re.Match, url_group: int) -> str:
        nonlocal replaced
        raw_url = match.group(url_group)
        full_url = urljoin(base_css_url, raw_url.strip())
        local = resource_map.get(full_url)
        if not local:
            return match.group(0)
        rel = relative_path_fn(css_local_path, local)
        replaced += 1
        return match.group(0).replace(raw_url, rel)

    def replace_import(match: re.Match) -> str:
        return resolve_and_replace(match, 1)

    def replace_url(match: re.Match) -> str:
        return resolve_and_replace(match, 1)

    css_text = CSS_IMPORT_RE.sub(replace_import, css_text)
    css_text = CSS_URL_RE.sub(replace_url, css_text)

    return css_text, replaced
