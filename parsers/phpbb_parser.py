"""
phpbb_parser.py — извлечение структуры форума из HTML-страниц phpBB.

Ключевое архитектурное решение: парсинг опирается на ПАТТЕРНЫ URL
(viewforum.php?f=N, viewtopic.php?t=N, ...start=N), а не на CSS-классы
конкретной темы оформления. Тема форума — "AllanStyle-SUBSILVER 3.2.1",
кастомная надстройка над стандартным subsilver2, поэтому точные названия
классов могут отличаться от стокового phpBB, а параметры URL в стандартных
контроллерах (viewforum.php, viewtopic.php) остаются одинаковыми независимо
от темы оформления. Это делает парсер устойчивым к особенностям конкретной
темы форума.

Если после первого тестового прогона (test_mode) окажется, что часть данных
(например, автор темы) не извлекается из-за нестандартной верстки —
эти функции можно точечно доработать под реальную HTML-структуру.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup


FORUM_LINK_RE = re.compile(r"viewforum\.php\?.*?\bf=(\d+)")
TOPIC_LINK_RE = re.compile(r"viewtopic\.php\?.*?\bt=(\d+)")
START_PARAM_RE = re.compile(r"[?&]start=(\d+)")


@dataclass
class ForumLink:
    forum_id: str
    name: str
    url: str


@dataclass
class TopicLink:
    topic_id: str
    name: str
    url: str
    author: Optional[str] = None


def _extract_id(pattern: re.Pattern, href: str) -> Optional[str]:
    m = pattern.search(href)
    return m.group(1) if m else None


def extract_forum_links(html: str, base_url: str) -> list[ForumLink]:
    """
    Находит все ссылки на разделы/подразделы (viewforum.php?f=N) на странице.
    Если один и тот же раздел упомянут несколько раз (например, иконка +
    текстовая ссылка), берется вариант с самым длинным непустым текстом —
    это обычно и есть настоящее название раздела, а не иконка "непрочитано".
    """
    soup = BeautifulSoup(html, "lxml")
    best_by_id: dict[str, ForumLink] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        forum_id = _extract_id(FORUM_LINK_RE, href)
        if not forum_id:
            continue
        text = a.get_text(strip=True)
        if not text:
            continue
        full_url = urljoin(base_url, href.replace("&amp;", "&"))
        existing = best_by_id.get(forum_id)
        if existing is None or len(text) > len(existing.name):
            best_by_id[forum_id] = ForumLink(forum_id=forum_id, name=text, url=full_url)

    return list(best_by_id.values())


def extract_topic_links(html: str, base_url: str) -> list[TopicLink]:
    """
    Находит темы на странице раздела.

    Основной путь — ищем ссылки с классом "topictitle": это стандартный
    класс заголовка темы и в prosilver, и в subsilver2, устойчивый к
    кастомизации темы оформления. Это важно, потому что на странице
    раздела на одну и ту же тему (один и тот же t=N) обычно ссылаются
    НЕСКОЛЬКО раз — сам заголовок, ссылка на дату последнего сообщения,
    номера страниц пагинации темы — и текст этих ссылок бывает длиннее
    текста заголовка, так что ориентироваться на "самый длинный текст"
    ненадежно.

    Автор темы (создатель, а не автор последнего ответа) ищется в блоке
    с классом "topic-poster" в пределах той же строки — это тоже
    стандартный класс phpBB, отдельный от блока "последнее сообщение".
    """
    soup = BeautifulSoup(html, "lxml")
    results: dict[str, TopicLink] = {}

    title_links = soup.find_all("a", class_="topictitle")
    for a in title_links:
        href = a.get("href")
        if not href:
            continue
        topic_id = _extract_id(TOPIC_LINK_RE, href)
        if not topic_id or topic_id in results:
            continue
        text = a.get_text(strip=True)
        if not text:
            continue
        full_url = urljoin(base_url, href.replace("&amp;", "&"))

        author = None
        container = a.find_parent("li") or a.find_parent("tr")
        if container is not None:
            poster_block = container.find(class_=re.compile(r"\btopic-poster\b"))
            search_scope = poster_block if poster_block is not None else container
            profile_link = search_scope.find("a", href=re.compile(r"memberlist\.php\?.*mode=viewprofile"))
            if profile_link is not None:
                author = profile_link.get_text(strip=True) or None

        results[topic_id] = TopicLink(topic_id=topic_id, name=text, url=full_url, author=author)

    if results:
        return list(results.values())

    # Резервный путь для тем оформления без класса "topictitle" —
    # прежняя эвристика "самый длинный текст ссылки на тот же t=".
    best_by_id: dict[str, TopicLink] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        topic_id = _extract_id(TOPIC_LINK_RE, href)
        if not topic_id:
            continue
        text = a.get_text(strip=True)
        if not text:
            continue
        full_url = urljoin(base_url, href.replace("&amp;", "&"))

        author = None
        row = a.find_parent("tr") or a.find_parent("li")
        if row is not None:
            profile_link = row.find("a", href=re.compile(r"memberlist\.php\?.*mode=viewprofile"))
            if profile_link is not None:
                author = profile_link.get_text(strip=True) or None

        existing = best_by_id.get(topic_id)
        if existing is None or len(text) > len(existing.name):
            best_by_id[topic_id] = TopicLink(topic_id=topic_id, name=text, url=full_url, author=author)
        elif author and not existing.author:
            existing.author = author

    return list(best_by_id.values())


def discover_pagination_urls(html: str, current_url: str) -> list[str]:
    """
    Определяет список URL всех страниц пагинации (виджет "1 2 3 ... N")
    на основе значений параметра start=, встречающихся в ссылках на
    ТЕКУЩУЮ страницу (тот же путь и тот же id раздела/темы, что и в
    current_url). Не зависит от текста/класса пагинации — только от
    числовых значений start в URL, что устойчиво к любой кастомизации темы.

    Возвращает полный список URL страниц (включая текущую, start=0),
    отсортированный по возрастанию start.
    """
    parsed_current = urlparse(current_url)
    current_qs = parse_qs(parsed_current.query)
    current_script = parsed_current.path.rsplit("/", 1)[-1]

    id_param, id_value = None, None
    for key in ("f", "t"):
        if key in current_qs:
            id_param, id_value = key, current_qs[key][0]
            break
    if id_param is None:
        return [current_url]

    soup = BeautifulSoup(html, "lxml")
    starts: set[int] = {0}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Раздел (f=N) со страницей start=N в ссылке может встретиться и
        # на ссылке на КОНКРЕТНУЮ ТЕМУ этого раздела с её последней
        # прочитанной страницей (viewtopic.php?f=9&t=30&start=20) — у неё
        # тот же f=, но это НЕ страница пагинации СПИСКА тем раздела.
        # Сверяем ещё и сам скрипт в пути (viewforum.php для раздела,
        # viewtopic.php для темы), чтобы не путать эти два разных start=.
        href_path = urlparse(href).path
        href_script = href_path.rsplit("/", 1)[-1] if href_path else ""
        if href_script and href_script != current_script:
            continue
        if f"{id_param}={id_value}" not in href:
            continue
        m = START_PARAM_RE.search(href)
        if m:
            starts.add(int(m.group(1)))

    sorted_starts = sorted(starts)
    if len(sorted_starts) <= 1:
        return [current_url]

    # Шаг между страницами (сколько тем/сообщений на странице) выводится
    # из минимальной положительной разницы между найденными значениями start.
    diffs = [b - a for a, b in zip(sorted_starts, sorted_starts[1:]) if b - a > 0]
    step = min(diffs) if diffs else sorted_starts[-1]
    max_start = sorted_starts[-1]

    urls = []
    start = 0
    while start <= max_start:
        urls.append(_set_start_param(current_url, start))
        start += step
    return urls


def _set_start_param(url: str, start: int) -> str:
    """Возвращает URL с добавленным/замененным параметром start=N."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs["start"] = [str(start)]
    query = "&".join(f"{k}={v[0]}" for k, v in qs.items())
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{query}"


def find_next_page_url(html: str, current_url: str) -> Optional[str]:
    """
    Возвращает URL следующей страницы (для инкрементального обнаружения
    страниц темы во время скачивания, без предварительного полного скана
    пагинации). Использует ту же логику start=, что и discover_pagination_urls,
    но возвращает только следующий шаг относительно текущего start.
    """
    parsed_current = urlparse(current_url)
    current_qs = parse_qs(parsed_current.query)
    current_start = int(current_qs.get("start", ["0"])[0])

    all_pages = discover_pagination_urls(html, current_url)
    all_starts = sorted(
        int(parse_qs(urlparse(u).query).get("start", ["0"])[0]) for u in all_pages
    )
    for s in all_starts:
        if s > current_start:
            return _set_start_param(current_url, s)
    return None


# ---------------------------------------------------------------------------
# Ресурсы: изображения, вложения, аватары, смайлики, CSS
# ---------------------------------------------------------------------------

RESOURCE_TYPE_AVATAR = "avatar"
RESOURCE_TYPE_ATTACHMENT = "attachment"
RESOURCE_TYPE_SMILEY = "smiley"
RESOURCE_TYPE_IMAGE = "image"
RESOURCE_TYPE_CSS = "css"

AVATAR_URL_RE = re.compile(r"(avatars?/|[?&]avatar=)", re.IGNORECASE)
ATTACHMENT_URL_RE = re.compile(r"download/file\.php\?", re.IGNORECASE)
SMILEY_URL_RE = re.compile(r"(smilies/|images/smilies)", re.IGNORECASE)
# Расширения, которые заведомо не являются полезными ресурсами архива
# (иконки интерфейса phpBB, счетчики, трекеры и т.п.) — пропускаем, чтобы
# не засорять архив служебной графикой темы оформления.
SKIP_IMAGE_PATTERNS = re.compile(
    r"(icon_|spacer\.gif|blank\.gif|/assets/|favicon|font-awesome)", re.IGNORECASE
)


@dataclass
class ResourceLink:
    type: str   # avatar | attachment | smiley | image | css
    url: str


def extract_resource_links(html: str, base_url: str) -> list[ResourceLink]:
    """
    Находит ссылки на изображения, вложения, аватары, смайлики и CSS на
    сохраненной странице — для последующей постановки в очередь на
    скачивание (режим 2 "Полный архив").

    Классификация — по паттерну URL, не по CSS-классам темы оформления:
        - avatar: содержит "avatar" в пути/параметрах (стандартный путь
          phpBB для аватаров, включая download/file.php?avatar=N);
        - attachment: download/file.php?... без avatar= — стандартная
          ссылка на вложение phpBB;
        - smiley: путь содержит "smilies/" — стандартная папка смайликов;
        - image: любой другой <img src="..."> на странице (инлайн-картинки
          сообщений, включая отрендеренный BBCode [img]) — за вычетом
          служебной графики темы оформления (иконки, spacer.gif и т.п.);
        - css: <link rel="stylesheet" href="...">.

    Дубликаты (один и тот же URL несколько раз на странице) убираются.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    results: list[ResourceLink] = []

    def add(resource_type: str, raw_url: str) -> None:
        if not raw_url:
            return
        full_url = urljoin(base_url, raw_url.replace("&amp;", "&"))
        if full_url in seen:
            return
        seen.add(full_url)
        results.append(ResourceLink(type=resource_type, url=full_url))

    for img in soup.find_all("img", src=True):
        src = img["src"]
        if AVATAR_URL_RE.search(src):
            add(RESOURCE_TYPE_AVATAR, src)
        elif SMILEY_URL_RE.search(src):
            add(RESOURCE_TYPE_SMILEY, src)
        elif SKIP_IMAGE_PATTERNS.search(src):
            continue
        else:
            add(RESOURCE_TYPE_IMAGE, src)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ATTACHMENT_URL_RE.search(href) and not AVATAR_URL_RE.search(href):
            add(RESOURCE_TYPE_ATTACHMENT, href)

    for link in soup.find_all("link", rel="stylesheet", href=True):
        add(RESOURCE_TYPE_CSS, link["href"])

    return results
