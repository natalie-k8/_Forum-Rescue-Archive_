"""
test_forum2x2_parser.py — проверка извлечения структуры форума forum2x2.ru.

Часть 1 — все функции (extract_forum_links, extract_topic_links,
discover_pagination_urls, find_next_page_url, extract_resource_links)
проверяются на РЕАЛЬНЫХ сохранённых страницах: главная форума
(sample_forum2x2_home.html), страница раздела со списком тем
(sample_forum2x2_viewforum.html) и страница темы с сообщениями
(sample_forum2x2_topic.html).

Часть 2 — несколько СИНТЕТИЧЕСКИХ фикстур для краевых случаев, которых нет
в присланных страницах (тема без пагинации вообще, повторная короткая
ссылка на тему без класса topictitle) — логика та же, что подтверждена
в части 1, просто нет реальной страницы именно с таким случаем под рукой.
"""


import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше tests/) — нужен, чтобы "from utils...."
# и подобные внутрипроектные импорты работали независимо от того, откуда
# запущен этот файл (py tests/test_x.py из корня, или py test_x.py изнутри tests/).

import os

from parsers.forum2x2_parser import (
    extract_forum_links,
    extract_topic_links,
    extract_resource_links,
    discover_pagination_urls,
    find_next_page_url,
)

BASE_URL = "https://example-forum2x2-sample.ru/"
SAMPLE_HOME_PATH = os.path.join(os.path.dirname(__file__), "sample_forum2x2_home.html")
SAMPLE_VIEWFORUM_PATH = os.path.join(os.path.dirname(__file__), "sample_forum2x2_viewforum.html")
SAMPLE_TOPIC_PATH = os.path.join(os.path.dirname(__file__), "sample_forum2x2_topic.html")


# ---------------------------------------------------------------------------
# Синтетические фикстуры для тем/пагинации/ресурсов (см. предупреждение выше)
# ---------------------------------------------------------------------------

VIEWFORUM_HTML = """
<html><body>
<ul>
  <li>
    <dd class="dterm">
      <a class="topictitle" href="/t100-eksponaty-muzeya-prado" title="">Экспонаты музея Прадо</a>
      <span class="span-tab">автор <a href="/u55">Иван Иванов</a></span>
    </dd>
    <dd class="lastpost">
      <a href="/u999" class="gensmall">Последний Отвечавший</a>
    </dd>
  </li>
  <li>
    <dd class="dterm">
      <a class="topictitle" href="/t101-istoriya-impressionizma" title="">История импрессионизма</a>
      <span class="span-tab">автор <a href="/u56">Мария Петрова</a></span>
    </dd>
  </li>
  <li>
    <!-- та же тема (t100) ещё раз упомянута короткой ссылкой без класса
         topictitle (например, дата последнего ответа) — НЕ должна создать
         дубликат и НЕ должна быть спутана с настоящим заголовком темы -->
    <a href="/t100p3-eksponaty-muzeya-prado#22656">03 авг 2025</a>
  </li>
</ul>
</body></html>
"""

TOPIC_PAGE1_HTML = """
<html><body>
<p>Сообщение №1 от автора темы...</p>
<div class="pagination">
  <a href="/t100-eksponaty-muzeya-prado">1</a>
  <a href="/t100p20-eksponaty-muzeya-prado">2</a>
  <a href="/t100p40-eksponaty-muzeya-prado">3</a>
</div>
</body></html>
"""

TOPIC_SINGLE_PAGE_HTML = """
<html><body>
<p>Тема без пагинации — все сообщения на одной странице.</p>
</body></html>
"""

RESOURCE_HTML = """
<html><body>
<img src="https://i.servimg.com/u/f74/12/94/53/72/photo.jpg" alt="">
<img src="https://x.illipro.net/i/icons/unread.gif" alt="">
<link rel="stylesheet" href="/1-ltr.css">
</body></html>
"""


def test_real_home_page_forums_and_resources():
    with open(SAMPLE_HOME_PATH, encoding="utf-8") as f:
        html = f.read()

    forums = extract_forum_links(html, BASE_URL)
    assert len(forums) > 0, "На реальной главной странице не найдено ни одного раздела/категории"
    by_id = {f.forum_id: f for f in forums}
    # id категории/раздела с буквенным префиксом, чтобы не путать c1 и f1
    assert "c1" in by_id, "Категория верхнего уровня (c{id}) не найдена"
    assert "f3" in by_id
    print(f"[OK] На реальной главной странице найдено {len(forums)} разделов/категорий "
          f"(включая категорию верхнего уровня '{by_id['c1'].name}')")

    resources = extract_resource_links(html, BASE_URL)
    types = {r.type for r in resources}
    assert "image" in types, "Пользовательская картинка (servimg.com) не найдена"
    assert "css" in types, "Стили не найдены"
    servimg_found = any("servimg.com" in r.url for r in resources)
    illipro_found = any("illipro.net" in r.url for r in resources)
    assert servimg_found, "Картинка с servimg.com должна попадать в ресурсы"
    assert not illipro_found, "Графика темы оформления (illipro.net) должна отфильтровываться"
    print(f"[OK] На реальной главной странице найдено {len(resources)} ресурсов "
          f"({', '.join(sorted(types))}), графика темы оформления (illipro.net) отфильтрована")


def test_real_viewforum_page_topics():
    with open(SAMPLE_VIEWFORUM_PATH, encoding="utf-8") as f:
        html = f.read()

    topics = extract_topic_links(html, BASE_URL)
    # Проверено вручную: на этой странице ровно 2 настоящие темы (2 блока
    # class="row row1"/"row row2" с a.topictitle) — остальные /t{id}-
    # ссылки на странице (их 12) относятся к виджету "последние сообщения"
    # сбоку и НЕ должны попадать в список тем.
    assert len(topics) == 2, f"Ожидалось 2 темы, получено {len(topics)}: {[t.name for t in topics]}"
    by_id = {t.topic_id: t for t in topics}
    assert by_id["28"].name == "Цвет, аккорд и руны"
    assert by_id["20"].name == "Теория музыки и позвоночник"
    assert by_id["28"].author is not None, "Автор темы 28 не найден"
    assert by_id["20"].author is not None, "Автор темы 20 не найден"
    print(f"[OK] На реальной странице раздела найдено ровно {len(topics)} настоящие темы "
          f"(виджет 'последние сообщения' с ещё 10 лишними /t{{id}}- ссылками корректно проигнорирован), "
          f"автор определён для обеих")


def test_real_topic_page_pagination_and_resources():
    with open(SAMPLE_TOPIC_PATH, encoding="utf-8") as f:
        html = f.read()

    # Реальный topic_id этой страницы — 28 (та же тема "Цвет, аккорд и руны")
    current_url = BASE_URL.rstrip("/") + "/t28-topic-slug"
    pages = discover_pagination_urls(html, current_url)
    # Подтверждено текстом на странице "Страница 1 из 5"
    assert len(pages) == 5, f"Ожидалось 5 страниц темы, получено {len(pages)}: {pages}"
    assert pages[0].endswith("/t28-topic-slug")
    assert pages[-1].endswith("t28p132-topic-slug")
    print(f"[OK] На реальной странице темы пагинация определена верно: {len(pages)} страниц "
          f"(совпадает с 'Страница 1 из 5' на странице), номера страниц — не 1/2/3, "
          f"а кратные размеру страницы (33/66/99/132), как offset у phpBB")

    next_url = find_next_page_url(html, current_url)
    assert next_url is not None and next_url.endswith("t28p33-topic-slug"), next_url
    print(f"[OK] find_next_page_url на реальной странице вернул: {next_url}")

    resources = extract_resource_links(html, BASE_URL)
    types = {r.type for r in resources}
    assert "avatar" in types, "Аватары (7img.net, путь /avatars/) должны определяться отдельным типом"
    assert "image" in types, "Обычные картинки в сообщениях должны находиться"
    # Хотлинки с внешних фотохостингов (не платформы forum2x2) — тоже должны
    # скачиваться, а не игнорироваться как что-то стороннее.
    hotlinked_hosts = {"radikal.ru", "postimages.org"}
    found_hotlinked = [r for r in resources if any(h in r.url for h in hotlinked_hosts)]
    assert found_hotlinked, "Картинки, вставленные хотлинком с radikal.ru/postimages.org, должны попадать в ресурсы"
    illipro_found = any("illipro.net" in r.url for r in resources)
    assert not illipro_found, "Графика темы оформления (illipro.net), включая заглушку аватара, должна отфильтровываться"
    print(f"[OK] На реальной странице темы найдено {len(resources)} ресурсов ({', '.join(sorted(types))}), "
          f"включая {len(found_hotlinked)} хотлинк(а/ов) с внешних фотохостингов (radikal.ru/postimages.org), "
          f"графика темы оформления отфильтрована")


def main():
    test_real_home_page_forums_and_resources()
    test_real_viewforum_page_topics()
    test_real_topic_page_pagination_and_resources()

    # --- ниже — синтетические проверки, см. предупреждение в шапке файла ---

    topics = extract_topic_links(VIEWFORUM_HTML, BASE_URL)
    topics_by_id = {t.topic_id: t for t in topics}
    assert set(topics_by_id.keys()) == {"100", "101"}, topics_by_id.keys()
    assert topics_by_id["100"].name == "Экспонаты музея Прадо", \
        f"Взято не то название темы: {topics_by_id['100'].name!r}"
    assert topics_by_id["100"].author == "Иван Иванов"
    assert topics_by_id["101"].author == "Мария Петрова"
    assert topics_by_id["100"].url == BASE_URL.rstrip("/") + "/t100-eksponaty-muzeya-prado", \
        f"Ссылка на тему должна быть приведена к 1-й странице: {topics_by_id['100'].url!r}"
    print(f"[СИНТЕТИКА, требует сверки на реальной странице] Найдено {len(topics)} темы с авторами, "
          f"повторная короткая ссылка (p3, без текста темы) не создала дубликат")

    pages = discover_pagination_urls(TOPIC_PAGE1_HTML, BASE_URL + "t100-eksponaty-muzeya-prado")
    assert len(pages) == 3, pages
    assert pages[0].endswith("t100-eksponaty-muzeya-prado")
    assert pages[1].endswith("t100p20-eksponaty-muzeya-prado")
    assert pages[2].endswith("t100p40-eksponaty-muzeya-prado")
    print(f"[СИНТЕТИКА] Пагинация темы определена верно: {len(pages)} страницы, шаг восстановлен "
          f"из /t{{id}}p{{N}}- (номер — offset вида posts_per_page*N, как start= у phpBB, не 1/2/3)")

    pages_single = discover_pagination_urls(TOPIC_SINGLE_PAGE_HTML, BASE_URL + "t200-tema-bez-otvetov")
    assert len(pages_single) == 1
    print("[СИНТЕТИКА] Тема без пагинации корректно определена как одна страница")

    next_url = find_next_page_url(TOPIC_PAGE1_HTML, BASE_URL + "t100-eksponaty-muzeya-prado")
    assert next_url is not None and next_url.endswith("t100p20-eksponaty-muzeya-prado"), next_url
    print(f"[СИНТЕТИКА] find_next_page_url вернул правильный URL следующей страницы: {next_url}")

    next_url_last = find_next_page_url(TOPIC_PAGE1_HTML, BASE_URL + "t100p40-eksponaty-muzeya-prado")
    assert next_url_last is None
    print("[СИНТЕТИКА] На последней странице find_next_page_url корректно возвращает None")

    resources = extract_resource_links(RESOURCE_HTML, BASE_URL)
    types = [r.type for r in resources]
    assert types.count("image") == 1, "Должна остаться только 1 картинка (servimg), illipro.net отфильтрован"
    assert types.count("css") == 1
    print(f"[СИНТЕТИКА] Ресурсы: {len(resources)} шт., графика темы оформления отфильтрована")

    print("\nВСЕ ПРОВЕРКИ forum2x2_parser.py ПРОЙДЕНЫ (часть — на реальной странице, "
          "часть — на синтетике, ждёт сверки на реальной странице раздела/темы).")


if __name__ == "__main__":
    main()
