"""
forum2x2_parser.py — извлечение структуры форума из HTML-страниц движка
форумов forum2x2.ru (форк/реплика phpBB c человекочитаемыми URL вместо
query-параметров).

Даёт ТОТ ЖЕ интерфейс, что и phpbb_parser.py (те же имена функций, те же
dataclass'ы ForumLink/TopicLink/ResourceLink), чтобы Scanner и Downloader
могли работать с любым из двух модулей без изменений — какой парсер
использовать, будет выбираться в конфиге/UI на этапе 6.

Ключевое отличие от phpbb_parser.py: там, где phpBB кодирует раздел/тему/
страницу в query-параметрах (viewforum.php?f=, viewtopic.php?t=, &start=),
forum2x2 кодирует их прямо в пути URL:

    /f{forum_id}-название-раздела          — раздел
    /c{category_id}-название-категории     — категория (контейнер разделов)
    /t{topic_id}-название-темы             — тема, страница 1
    /t{topic_id}p{page}-название-темы      — тема, страница {page}
    /u{user_id}                            — профиль пользователя (автор)

Эти паттерны подтверждены на реальных сохранённых страницах (главная
форума, страница раздела со списком тем, страница темы с сообщениями),
включая CSS-класс "topictitle" для заголовка темы (тот же, что у phpBB) и
путь "/avatars/" для аватаров — подробности см. в докстрингах
extract_topic_links()/extract_resource_links() ниже. Что ещё НЕ проверено —
отмечено там же как TODO (в первую очередь — вложения, форма входа).
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from parsers.phpbb_parser import (
    ForumLink,
    TopicLink,
    ResourceLink,
    RESOURCE_TYPE_IMAGE,
    RESOURCE_TYPE_AVATAR,
    RESOURCE_TYPE_CSS,
)

# --- паттерны URL (подтверждены на реальной странице форума) ---------------

# У forum2x2 (в отличие от phpBB) главная страница форума — это сам
# base_url, без index.php. Scanner._bootstrap_root_sections() читает эту
# константу через getattr(parser, "HOME_PAGE_PATH", "index.php") — для
# phpbb_parser.py ничего менять не нужно (там используется дефолт).
HOME_PAGE_PATH = ""

# Аналогично — какой метод ForumClient вызывать для (повторного) входа.
# Downloader._login() читает это через
# getattr(parser, "LOGIN_METHOD_NAME", "login").
LOGIN_METHOD_NAME = "login_forum2x2"

FORUM_PATH_RE = re.compile(r"^/f(\d+)-")
CATEGORY_PATH_RE = re.compile(r"^/c(\d+)-")
TOPIC_PATH_RE = re.compile(r"^/t(\d+)(?:p(\d+))?(?:-|$)")
PROFILE_PATH_RE = re.compile(r"^/u(\d+)(?:$|[/?#])")

# Хост статики темы оформления (иконки, спрайты, favicon) — подтверждено
# на реальной странице (illipro.net) — пропускаем, как и icon_/spacer.gif
# у phpBB, чтобы не засорять архив служебной графикой.
SKIP_IMAGE_HOST_RE = re.compile(r"(^|\.)illipro\.net$", re.IGNORECASE)
# Путь аватара — подтверждено на реальной странице темы (скрипт_темы.txt):
# https://7img.net/users/.../avatars/5-71.jpg — сегмент "/avatars/" в пути,
# ХОСТ ПРИ ЭТОМ ДРУГОЙ (7img.net, не servimg.com, как у обычных картинок в
# сообщениях) — поэтому определяем по пути, а не по хосту.
AVATAR_PATH_RE = re.compile(r"/avatars?/", re.IGNORECASE)


def _path_only(href: str) -> str:
    """Возвращает путь без домена/query/fragment, чтобы применять regex к
    относительным и абсолютным ссылкам одинаково."""
    return urlparse(href).path


# ---------------------------------------------------------------------------
# Разделы и категории
# ---------------------------------------------------------------------------

def extract_forum_links(html: str, base_url: str) -> list[ForumLink]:
    """
    Находит ссылки на разделы (/f{id}-...) И категории (/c{id}-...).

    Категории здесь трактуются как обычные "разделы" для Scanner'а: у
    forum2x2 категория — это просто страница, на которой перечислены
    вложенные разделы, без собственных тем — так же, как у phpBB раздел
    может содержать только подразделы. Чтобы id не пересекались с id
    настоящих разделов, категориям присваивается forum_id вида "c123",
    разделам — "f123" (совпадает со стандартной нумерацией на сайте, но
    с буквенным префиксом для однозначности в БД).

    Подтверждено на реальной странице: один и тот же раздел может быть
    упомянут дважды (например, полное название рядом с заголовком секции
    и сокращённая ссылка в перечислении подразделов через запятую) —
    как и в phpbb_parser.py, берётся вариант с самым длинным текстом.
    """
    soup = BeautifulSoup(html, "lxml")
    best_by_id: dict[str, ForumLink] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        path = _path_only(href)

        m = FORUM_PATH_RE.match(path)
        prefix = "f"
        if not m:
            m = CATEGORY_PATH_RE.match(path)
            prefix = "c"
        if not m:
            continue

        text = a.get_text(strip=True)
        if not text:
            continue

        forum_id = f"{prefix}{m.group(1)}"
        full_url = urljoin(base_url, href)
        existing = best_by_id.get(forum_id)
        if existing is None or len(text) > len(existing.name):
            best_by_id[forum_id] = ForumLink(forum_id=forum_id, name=text, url=full_url)

    return list(best_by_id.values())


# ---------------------------------------------------------------------------
# Темы
# ---------------------------------------------------------------------------

def extract_topic_links(html: str, base_url: str) -> list[TopicLink]:
    """
    Находит темы на странице раздела.

    Основной путь — ищем ссылки с классом "topictitle": ПОДТВЕРЖДЕНО на
    реальной странице раздела (скрипт_подфорума.txt) — это тот же класс,
    что и у phpBB, forum2x2 использует ту же вёрстку списка тем (только
    вместо viewtopic.php?t= — путь /t{id}-slug). Без этого — если брать
    "любую ссылку /t{id}-..." — на странице раздела ловится куча лишнего:
    виджет "последние сообщения" сбоку, похожие темы и т.п. — на реальной
    странице так нашлось 12 "тем" вместо настоящих 2.

    Автор темы (создатель, а не автор последнего ответа) ищется в пределах
    ближайшего родителя <dd> — ПОДТВЕРЖДЕНО: строка темы forum2x2 обёрнута
    в <dd class="dterm">, внутри которого и заголовок, и блок "автор
    <a href='/u{id}'>", а блок "последнее сообщение" (<dd class="lastpost">)
    — отдельный сосед по <dl>, а не потомок этого <dd>, так что перепутать
    автора темы с автором последнего ответа тут не получится (та же защита,
    что и у phpBB с "topic-poster").
    """
    soup = BeautifulSoup(html, "lxml")
    results: dict[str, TopicLink] = {}

    title_links = soup.find_all("a", class_="topictitle")
    for a in title_links:
        href = a.get("href")
        if not href:
            continue
        m = TOPIC_PATH_RE.match(_path_only(href))
        if not m or m.group(1) in results:
            continue
        topic_id = m.group(1)
        text = a.get_text(strip=True)
        if not text:
            continue
        canonical_url = _canonical_topic_page1_url(href, base_url)

        author = None
        container = a.find_parent("dd") or a.find_parent("li") or a.find_parent("tr")
        if container is not None:
            profile_link = container.find("a", href=PROFILE_PATH_RE)
            if profile_link is not None:
                author = profile_link.get_text(strip=True) or None

        results[topic_id] = TopicLink(topic_id=topic_id, name=text, url=canonical_url, author=author)

    if results:
        return list(results.values())

    # Резервный путь для случаев, когда класс "topictitle" почему-то не
    # найден (нестандартная кастомизация подраздела) — прежняя широкая
    # эвристика "любая ссылка /t{id}-...", с выбором самого длинного текста.
    best_by_id: dict[str, TopicLink] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        path = _path_only(href)
        m = TOPIC_PATH_RE.match(path)
        if not m:
            continue
        topic_id = m.group(1)

        text = a.get_text(strip=True)
        if not text:
            continue

        canonical_url = _canonical_topic_page1_url(href, base_url)

        author = None
        container = a.find_parent("li") or a.find_parent("tr") or a.find_parent("dd") or a.find_parent("div")
        if container is not None:
            profile_link = container.find("a", href=PROFILE_PATH_RE)
            if profile_link is not None:
                author = profile_link.get_text(strip=True) or None

        existing = best_by_id.get(topic_id)
        if existing is None or len(text) > len(existing.name):
            best_by_id[topic_id] = TopicLink(topic_id=topic_id, name=text, url=canonical_url, author=author)
        elif author and not existing.author:
            existing.author = author

    return list(best_by_id.values())


def _canonical_topic_page1_url(href: str, base_url: str) -> str:
    """Приводит ссылку на тему (с любой страницы p{N} и с любым #fragment)
    к канонической ссылке на страницу 1 той же темы: /t{id}-slug."""
    full_url = urljoin(base_url, href)
    parsed = urlparse(full_url)
    m = TOPIC_PATH_RE.match(parsed.path)
    if not m:
        return full_url
    topic_id = m.group(1)
    slug = _slug_after_topic_prefix(parsed.path, topic_id)
    page1_path = f"/t{topic_id}{('-' + slug) if slug else ''}"
    return f"{parsed.scheme}://{parsed.netloc}{page1_path}"


def _slug_after_topic_prefix(path: str, topic_id: str) -> str:
    """Возвращает часть пути после '/t{id}' или '/t{id}p{N}-' (сам текстовый
    slug темы, используется для восстановления URL других страниц той же
    темы)."""
    m = re.match(rf"^/t{topic_id}(?:p\d+)?-?(.*)$", path)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Пагинация темы
# ---------------------------------------------------------------------------

def discover_pagination_urls(html: str, current_url: str) -> list[str]:
    """
    Определяет список URL всех страниц темы, встречающихся на странице —
    по номерам, зашитым прямо в путь (/t{id}p{N}-...), а не по значению
    query-параметра, как у phpBB.

    ВАЖНО (подтверждено на реальной странице темы, скрипт_темы.txt): номер
    в /t{id}p{N}- у forum2x2 — это НЕ порядковый номер страницы (1, 2, 3...),
    а величина, кратная числу сообщений на странице (например, 33, 66, 99,
    132 при 33 сообщениях на странице для 5-страничной темы) — по сути то
    же самое, что смещение "start=" у phpBB, только записанное иначе.
    Страница 1 суффикса не имеет вообще (просто /t{id}-slug) — её нельзя
    ставить в один ряд со значениями типа 33/66/99 при вычислении шага,
    иначе шаг посчитается неверно (33 - 1 = 32, а не 33). Поэтому шаг
    считается только по номерованным страницам (2-я и далее), а страница 1
    добавляется в результат отдельно, всегда.
    """
    parsed_current = urlparse(current_url)
    m = TOPIC_PATH_RE.match(parsed_current.path)
    if not m:
        return [current_url]
    topic_id = m.group(1)
    slug = _slug_after_topic_prefix(parsed_current.path, topic_id)

    soup = BeautifulSoup(html, "lxml")
    numbered_pages: set[int] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        path = _path_only(href)
        pm = TOPIC_PATH_RE.match(path)
        if not pm or pm.group(1) != topic_id or not pm.group(2):
            continue
        numbered_pages.add(int(pm.group(2)))

    if not numbered_pages:
        return [current_url]

    sorted_numbered = sorted(numbered_pages)
    diffs = [b - a for a, b in zip(sorted_numbered, sorted_numbered[1:]) if b - a > 0]
    step = min(diffs) if diffs else sorted_numbered[0]
    max_page = sorted_numbered[-1]

    urls = [_build_topic_page_url(parsed_current, topic_id, slug, 1)]
    page = sorted_numbered[0]
    while page <= max_page:
        urls.append(_build_topic_page_url(parsed_current, topic_id, slug, page))
        page += step
    return urls


def _build_topic_page_url(parsed_current, topic_id: str, slug: str, page: int) -> str:
    if page <= 1:
        path = f"/t{topic_id}{('-' + slug) if slug else ''}"
    else:
        path = f"/t{topic_id}p{page}{('-' + slug) if slug else ''}"
    return f"{parsed_current.scheme}://{parsed_current.netloc}{path}"


def find_next_page_url(html: str, current_url: str) -> Optional[str]:
    """Как в phpbb_parser.py: следующая страница относительно текущей,
    для инкрементального обнаружения во время скачивания."""
    parsed_current = urlparse(current_url)
    m = TOPIC_PATH_RE.match(parsed_current.path)
    current_page = int(m.group(2)) if (m and m.group(2)) else 1

    all_pages = discover_pagination_urls(html, current_url)
    all_page_numbers = sorted(
        int(TOPIC_PATH_RE.match(urlparse(u).path).group(2) or 1) for u in all_pages
    )
    for p in all_page_numbers:
        if p > current_page:
            slug = _slug_after_topic_prefix(parsed_current.path, m.group(1))
            return _build_topic_page_url(parsed_current, m.group(1), slug, p)
    return None


# ---------------------------------------------------------------------------
# Ресурсы: изображения, вложения, аватары, CSS
# ---------------------------------------------------------------------------

def extract_resource_links(html: str, base_url: str) -> list[ResourceLink]:
    """
    Находит ресурсы для докачки.

    Правило простое и проверенное на реальных страницах (главная, раздел,
    тема): берём ЛЮБУЮ картинку (<img src>), КРОМЕ хостов из
    SKIP_IMAGE_HOST_RE (графика темы оформления, illipro.net) — то есть
    "скачиваем всё, что не входит в чёрный список", а не "скачиваем только
    известные хосты пользовательского контента". Это важно, потому что на
    реальной странице темы (скрипт_темы.txt) обнаружилось, что пользователи
    вставляют картинки в сообщения не только через штатную загрузку форума
    (i.servimg.com), но и хотлинком с внешних фотохостингов — на этой же
    странице встретились postimages.org и несколько поддоменов radikal.ru
    (популярный в рунете старый фотохостинг, тоже может исчезнуть в любой
    момент) — их обязательно нужно скачивать наравне со "своими" картинками.

    Типы:
      - RESOURCE_TYPE_AVATAR — если в пути картинки есть "/avatars/"
        (подтверждено: https://7img.net/users/.../avatars/5-71.jpg — хост
        аватаров, 7img.net, ОТЛИЧАЕТСЯ от хоста обычных картинок в
        сообщениях, i.servimg.com, поэтому тип определяется по пути,
        а не по хосту);
      - RESOURCE_TYPE_IMAGE — всё остальное (картинки в тексте сообщений);
      - RESOURCE_TYPE_CSS — <link rel="stylesheet">, как и у phpBB.

    ТРЕБУЕТ ПЕРЕПРОВЕРКИ: вложения (файлы, прикреплённые к сообщению, а не
    вставленные картинкой) — в присланной теме их не было, паттерн ссылки
    на скачивание вложения forum2x2 пока не подтверждён нигде. Как только
    попадётся тема с настоящим вложением — нужно прислать её отдельно.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    results: list[ResourceLink] = []

    def add(resource_type: str, raw_url: str) -> None:
        if not raw_url:
            return
        full_url = urljoin(base_url, raw_url)
        if full_url in seen:
            return
        host = urlparse(full_url).netloc
        if SKIP_IMAGE_HOST_RE.search(host):
            return
        seen.add(full_url)
        results.append(ResourceLink(type=resource_type, url=full_url))

    for img in soup.find_all("img", src=True):
        src = img["src"]
        full_url = urljoin(base_url, src)
        path = urlparse(full_url).path
        resource_type = RESOURCE_TYPE_AVATAR if AVATAR_PATH_RE.search(path) else RESOURCE_TYPE_IMAGE
        add(resource_type, src)

    for link in soup.find_all("link", rel="stylesheet", href=True):
        add(RESOURCE_TYPE_CSS, link["href"])

    return results
