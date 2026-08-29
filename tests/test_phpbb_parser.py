"""
test_phpbb_parser.py — проверка извлечения разделов/тем/пагинации на
синтетических HTML-фикстурах, приближенных к структуре subsilver2.

Примечание: реальная тема "AllanStyle-SUBSILVER 3.2.1" может иметь свои
отличия в верстке — парсер намеренно опирается на URL-паттерны
(viewforum.php?f=, viewtopic.php?t=, start=), а не на CSS-классы, поэтому
должен быть устойчив к таким отличиям. Если что-то не совпадет на реальном
форуме — пришли пример реальной HTML-страницы, поправим точечно.
"""


import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше tests/) — нужен, чтобы "from utils...."
# и подобные внутрипроектные импорты работали независимо от того, откуда
# запущен этот файл (py tests/test_x.py из корня, или py test_x.py изнутри tests/).

from parsers.phpbb_parser import extract_forum_links, extract_topic_links, discover_pagination_urls, find_next_page_url

BASE_URL = "http://forum.test/"

INDEX_HTML = """
<html><body>
<table class="forumline">
  <tr>
    <td><a href="./viewforum.php?f=1" class="forumlink">Общий раздел</a></td>
  </tr>
  <tr>
    <td>
      <a href="./viewforum.php?f=2" class="forumlink">Искусство и история</a>
      <span class="nest">
        Подфорумы:
        <a href="./viewforum.php?f=3">Живопись</a>,
        <a href="./viewforum.php?f=4">Архитектура</a>
      </span>
    </td>
  </tr>
  <tr>
    <!-- иконка "непрочитано" без текста — не должна попасть в результат как отдельный "раздел" -->
    <td><a href="./viewforum.php?f=1"><img src="unread.gif" alt=""/></a></td>
  </tr>
</table>
</body></html>
"""

VIEWFORUM_HTML = """
<html><body>
<table class="forumline">
  <tr>
    <td><a href="./viewtopic.php?f=2&amp;t=100" class="topictitle">Экспонаты музея Прадо</a></td>
    <td><a href="./memberlist.php?mode=viewprofile&amp;u=55">Иван Иванов</a></td>
  </tr>
  <tr>
    <td><a href="./viewtopic.php?f=2&amp;t=101" class="topictitle">История импрессионизма</a></td>
    <td><a href="./memberlist.php?mode=viewprofile&amp;u=56">Мария Петрова</a></td>
  </tr>
</table>
<div class="pagination">
  <a href="./viewforum.php?f=2&amp;start=0">1</a>,
  <a href="./viewforum.php?f=2&amp;start=25">2</a>,
  <a href="./viewforum.php?f=2&amp;start=50">3</a>
</div>
</body></html>
"""

VIEWTOPIC_PAGE1_HTML = """
<html><body>
<p>Сообщение №1 от автора темы...</p>
<div class="pagination">
  <a href="./viewtopic.php?f=2&amp;t=100&amp;start=0">1</a>,
  <a href="./viewtopic.php?f=2&amp;t=100&amp;start=15">2</a>
</div>
</body></html>
"""

VIEWTOPIC_SINGLE_PAGE_HTML = """
<html><body>
<p>Тема без пагинации — все сообщения на одной странице.</p>
</body></html>
"""


VIEWFORUM_PROSILVER_HTML = """
<html><body>
<ul class="topiclist topics">
  <li class="row bg1">
    <dl class="row-item topic_read_hot">
      <dt title="Нет непрочитанных сообщений">
        <div class="list-inner">
          <a href="./viewtopic.php?f=9&amp;t=30&amp;sid=xyz" class="topictitle">Настоящее название темы</a>
          <br />
          <div class="responsive-show" style="display: none;">
            Последнее сообщение  <a href="./memberlist.php?mode=viewprofile&amp;u=57&amp;sid=xyz" class="username-coloured">last_poster</a>
            &laquo; <a href="./viewtopic.php?f=9&amp;t=30&amp;p=61852&amp;sid=xyz#p61852" title="Перейти к последнему сообщению">03 авг 2025, 13:02, что длиннее заголовка</a>
          </div>
          <div class="topic-poster responsive-hide left-box">
             <a href="./memberlist.php?mode=viewprofile&amp;u=44&amp;sid=xyz" class="username">topic_starter</a> &raquo; 26 дек 2020, 02:00
          </div>
          <div class="pagination">
            <ul>
              <li><a class="button" href="./viewtopic.php?f=9&amp;t=30&amp;sid=xyz">1</a></li>
              <li><a class="button" href="./viewtopic.php?f=9&amp;t=30&amp;sid=xyz&amp;start=20">2</a></li>
            </ul>
          </div>
        </div>
      </dt>
      <dd class="lastpost">
        <span><a href="./memberlist.php?mode=viewprofile&amp;u=57&amp;sid=xyz" class="username-coloured">last_poster</a></span>
      </dd>
    </dl>
  </li>
</ul>
</body></html>
"""


def test_prosilver_topic_extraction():
    topics = extract_topic_links(VIEWFORUM_PROSILVER_HTML, BASE_URL)
    assert len(topics) == 1, topics
    topic = topics[0]
    assert topic.topic_id == "30"
    assert topic.name == "Настоящее название темы", \
        f"Взято не то название (перепутано с более длинной ссылкой на дату): {topic.name!r}"
    assert topic.author == "topic_starter", \
        f"Взят автор последнего сообщения вместо автора темы: {topic.author!r}"
    print("[OK] На реальной верстке prosilver (с 3+ ссылками на одну тему) "
          "верно выбраны название и именно автор темы, а не последнего ответа")


def main():
    # 1. Разделы, включая подфорумы, без дубликата из иконки
    forums = extract_forum_links(INDEX_HTML, BASE_URL)
    forums_by_id = {f.forum_id: f for f in forums}
    assert set(forums_by_id.keys()) == {"1", "2", "3", "4"}, forums_by_id.keys()
    assert forums_by_id["1"].name == "Общий раздел"
    assert forums_by_id["2"].name == "Искусство и история"
    assert forums_by_id["3"].name == "Живопись"
    assert forums_by_id["4"].name == "Архитектура"
    print(f"[OK] Найдено {len(forums)} раздела/подраздела, дубликат-иконка не создал лишнюю запись")

    # 2. Темы + авторы
    topics = extract_topic_links(VIEWFORUM_HTML, BASE_URL)
    topics_by_id = {t.topic_id: t for t in topics}
    assert set(topics_by_id.keys()) == {"100", "101"}
    assert topics_by_id["100"].name == "Экспонаты музея Прадо"
    assert topics_by_id["100"].author == "Иван Иванов"
    assert topics_by_id["101"].author == "Мария Петрова"
    print(f"[OK] Найдено {len(topics)} темы с авторами")

    # 3. Пагинация раздела: start=0,25,50 -> шаг 25, 3 страницы
    pages = discover_pagination_urls(VIEWFORUM_HTML, BASE_URL + "viewforum.php?f=2")
    assert len(pages) == 3, pages
    assert pages[0].endswith("start=0")
    assert pages[1].endswith("start=25")
    assert pages[2].endswith("start=50")
    print(f"[OK] Пагинация раздела определена верно: {len(pages)} страницы, шаг вычислен как 25")

    # 4. Пагинация темы: start=0,15 -> шаг 15, 2 страницы
    pages_topic = discover_pagination_urls(VIEWTOPIC_PAGE1_HTML, BASE_URL + "viewtopic.php?f=2&t=100")
    assert len(pages_topic) == 2
    assert pages_topic[1].endswith("start=15")
    print(f"[OK] Пагинация темы определена верно: {len(pages_topic)} страницы, шаг 15")

    # 5. Тема без пагинации -> одна страница
    pages_single = discover_pagination_urls(VIEWTOPIC_SINGLE_PAGE_HTML, BASE_URL + "viewtopic.php?f=2&t=200")
    assert len(pages_single) == 1
    print("[OK] Тема без пагинации корректно определена как одна страница")

    # 6. Инкрементальное определение следующей страницы (для Downloader)
    next_url = find_next_page_url(VIEWTOPIC_PAGE1_HTML, BASE_URL + "viewtopic.php?f=2&t=100&start=0")
    assert next_url is not None and next_url.endswith("start=15")
    print(f"[OK] find_next_page_url вернул правильный URL следующей страницы: {next_url}")

    next_url_last = find_next_page_url(VIEWTOPIC_PAGE1_HTML, BASE_URL + "viewtopic.php?f=2&t=100&start=15")
    assert next_url_last is None
    print("[OK] На последней странице find_next_page_url корректно возвращает None")

    print("\nВСЕ ПРОВЕРКИ phpbb_parser.py ПРОЙДЕНЫ УСПЕШНО.")

    test_prosilver_topic_extraction()


if __name__ == "__main__":
    main()
