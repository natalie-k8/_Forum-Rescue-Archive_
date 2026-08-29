"""
run_forum2x2_smoketest.py — пробный запуск forum2x2_parser.py на РЕАЛЬНОМ
форуме forum2x2.ru: одна страница, без сохранения в архив, без БД.

Это НЕ полноценный скан (как run_scan_test.py для первого форума) — до
этапа 6 Scanner и Downloader ещё не умеют выбирать парсер, и login() в
http_client.py написан под форму входа phpBB, которая у forum2x2, скорее
всего, устроена иначе (это отдельная задача, ещё не проверялась). Поэтому
этот скрипт запрашивает только ОДНУ публичную страницу без входа — чтобы
проверить extract_forum_links()/extract_resource_links() на реальной
верстке, прежде чем строить вокруг них Scanner/Downloader для второго
форума.

Если данные для входа в .env.forum2x2 заполнены — скрипт их прочитает и
покажет, что они найдены, но пробовать логиниться НЕ будет (см. выше).

Запуск (ничего редактировать в файле не нужно — всё спрашивается в консоли):

    py run_forum2x2_smoketest.py
"""


from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше run_commands/) — нужен, чтобы
# "from utils...." и подобные внутрипроектные импорты работали
# независимо от того, откуда запущен этот файл (через меню main.py,
# или напрямую: py run_commands/run_archive.py).


from utils.credentials import load_credentials
from network.http_client import ForumClient
from utils.logger_setup import setup_logging
from parsers.forum2x2_parser import extract_forum_links, extract_resource_links

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 ForumRescueArchive/1.0"
)


def normalize_base_url(url: str) -> str:
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    if not url.endswith("/"):
        url += "/"
    return url


def main():
    print("=== Forum Rescue Archive — пробный запуск forum2x2_parser.py ===\n")

    setup_logging("logs_forum2x2_smoketest")

    creds = load_credentials(".env.forum2x2", prefix="FORUM2X2")
    if creds.is_set():
        print("[i] Данные для входа в .env.forum2x2 найдены, но эта проверка "
              "их не использует — логин для forum2x2 ещё не реализован.\n")
    else:
        print("[i] .env.forum2x2 не заполнен — это нормально для этой "
              "проверки, вход пока не требуется.\n")

    raw_url = input("Адрес главной страницы форума (forum2x2.ru): ").strip()
    if not raw_url:
        print("Адрес не введён, выхожу.")
        return
    base_url = normalize_base_url(raw_url)

    client = ForumClient(base_url, USER_AGENT, min_delay=20.0, max_delay=40.0)

    print(f"\nЗапрашиваю {base_url} ...")
    result = client.fetch(base_url)

    if result.status != "success":
        print(f"[ОШИБКА] Не удалось получить страницу: "
              f"{result.status} ({result.error_message})")
        return

    print("[OK] Страница получена, разбираю...\n")

    forums = extract_forum_links(result.text, base_url)
    print(f"Найдено разделов/категорий: {len(forums)}")
    for f in forums[:15]:
        print(f"  {f.forum_id:>6}  {f.name}")
    if len(forums) > 15:
        print(f"  ... и ещё {len(forums) - 15}")

    resources = extract_resource_links(result.text, base_url)
    print(f"\nНайдено ресурсов на главной странице: {len(resources)}")
    for r in resources[:10]:
        print(f"  [{r.type}] {r.url}")

    print(
        "\nЭто была одна страница без сохранения. Если разделы/ресурсы "
        "выглядят правильно — можно прислать реальную страницу раздела со "
        "списком тем и страницу темы с сообщениями, чтобы сверить "
        "extract_topic_links()/discover_pagination_urls() (сейчас они "
        "проверены только синтетически, см. test_forum2x2_parser.py)."
    )


if __name__ == "__main__":
    main()
