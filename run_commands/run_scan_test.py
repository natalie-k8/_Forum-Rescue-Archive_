"""
run_scan_test.py — пробный запуск Scanner на РЕАЛЬНОМ форуме, в безопасном
тестовом режиме (ограниченное число разделов/тем).

Запуск (ничего редактировать в файле не нужно — все спрашивается в консоли):

    py run_scan_test.py

Если нужен логин для закрытых разделов — заполни .env заранее (см.
.env.example). Логин/пароль в консоли не запрашиваются намеренно —
чтобы пароль не оставался в истории команд.
"""


from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше run_commands/) — нужен, чтобы
# "from utils...." и подобные внутрипроектные импорты работали
# независимо от того, откуда запущен этот файл (через меню main.py,
# или напрямую: py run_commands/run_archive.py).


import shutil
import sys
from urllib.parse import urlparse

from utils.config import load_config, save_config, ensure_directories
from utils.credentials import load_credentials
from storage.database import connect_db, init_db, QueueManager
from network.http_client import ForumClient
from utils.logger_setup import setup_logging
from scanner.scanner import Scanner

TEST_CONFIG_PATH = "config_test_scan.json"
TEST_ARCHIVE_ROOT = "Archive_test_scan"


def ask_int(prompt: str, default: int) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit():
            return int(raw)
        print("Введите целое число или оставьте пустым для значения по умолчанию.")


def normalize_base_url(url: str) -> str:
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    # Если скопирован полный адрес главной страницы (с index.php) —
    # нужен именно корень форума, иначе все ссылки будут строиться неверно.
    for suffix in ("index.php/", "index.php"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    if not url.endswith("/"):
        url += "/"
    return url


def main():
    print("=== Forum Rescue Archive — пробное сканирование ===\n")

    cfg = load_config(TEST_CONFIG_PATH)
    previous_url = cfg.forum.base_url or None

    if previous_url:
        raw_url = input(f"Адрес форума [{previous_url}]: ").strip()
        if not raw_url:
            raw_url = previous_url
    else:
        raw_url = input("Адрес форума (например, http://myforum.ru/): ").strip()

    if not raw_url:
        print("Адрес форума обязателен. Запусти скрипт заново и введи адрес.")
        sys.exit(1)
    base_url = normalize_base_url(raw_url)

    reset_answer = input(
        "\nНачать пробное сканирование заново (удалить результат прошлой "
        "пробной попытки)? [Y/n]: "
    ).strip().lower()
    if reset_answer in ("", "y", "yes", "д", "да"):
        shutil.rmtree(TEST_ARCHIVE_ROOT, ignore_errors=True)
        print("Прошлые результаты пробного скана удалены — начинаю с чистого листа.")
    else:
        print("Продолжаю с того места, где остановилось прошлое пробное сканирование "
              "(уже отсканированные разделы повторно не тронет).")

    print("\nТестовый режим ограничивает объем сканирования, чтобы проверить")
    print("парсинг без лишней нагрузки на сервер. Можно просто нажать Enter")
    print("на каждом вопросе, чтобы взять безопасные значения по умолчанию.\n")

    max_sections = ask_int("Сколько разделов верхнего уровня сканировать", 5)
    max_topics = ask_int("Не более скольки тем на раздел", 3)
    max_tasks = ask_int("Общий лимит задач (страховка)", 30)

    cfg.forum.base_url = base_url
    cfg.paths.archive_root = TEST_ARCHIVE_ROOT
    cfg.test_mode.enabled = True
    cfg.test_mode.max_sections = max_sections
    cfg.test_mode.max_topics_per_section = max_topics
    cfg.test_mode.max_total_tasks = max_tasks
    save_config(cfg, TEST_CONFIG_PATH)
    ensure_directories(cfg)

    logger = setup_logging(cfg.paths.logs_dir())
    logger.info("=== Пробный запуск Scanner (test_mode) ===")

    conn = connect_db(cfg.paths.db_path())
    init_db(conn)
    qm = QueueManager(
        conn,
        backoff_schedule=cfg.backoff.backoff_schedule_seconds,
        reset_streak_after_successes=cfg.backoff.reset_streak_after_successes,
        max_attempts_per_task=cfg.retry.max_attempts_per_task,
        max_attempts_per_task_external=cfg.retry.max_attempts_per_task_external,
        own_host=urlparse(cfg.forum.base_url).netloc,
    )

    client = ForumClient(
        cfg.forum.base_url, cfg.forum.user_agent,
        cfg.throttle.min_delay_seconds, cfg.throttle.max_delay_seconds,
    )

    creds = load_credentials()
    if creds.is_set():
        print(f"\nНайдены учетные данные в .env — вхожу как {creds.username}...")
        try:
            ok = client.login(creds.username, creds.password)
            print("Логин выполнен успешно." if ok else "Логин НЕ выполнен — проверь логин/пароль в .env.")
        except Exception as e:
            print(f"Ошибка при попытке входа: {e}")
    else:
        print("\nДанные для входа не заданы (.env пуст) — будут просканированы только открытые разделы.")

    print(f"\nНачинаю сканирование: {base_url}")
    print(f"Лимиты теста: разделов={max_sections}, тем на раздел={max_topics}, всего задач={max_tasks}")
    print("Каждый запрос выполняется с паузой (см. config.json) — это может занять некоторое время.\n")

    scanner = Scanner(cfg, conn, qm, client)
    stats = scanner.run()

    print("\n=== Результат сканирования ===")
    print(f"Разделов найдено:          {stats.sections_discovered}")
    print(f"Тем найдено:                {stats.topics_discovered}")
    print(f"Задач на страницы раздела:  {stats.section_pages_enqueued}")
    print(f"Задач на страницы тем:      {stats.topic_pages_enqueued}")
    print(f"Причина остановки:          {stats.stopped_reason}")

    sections = conn.execute("SELECT name, url FROM sections").fetchall()
    print(f"\nНайденные разделы ({len(sections)}):")
    for s in sections:
        print(f"  - {s['name']}  ({s['url']})")

    topics = conn.execute("SELECT name, author, url FROM topics").fetchall()
    print(f"\nНайденные темы ({len(topics)}):")
    for t in topics:
        print(f"  - {t['name']}  [автор: {t['author']}]")

    print(f"\nБаза данных: {cfg.paths.db_path()}")
    print("Открой её через DB Browser for SQLite (бесплатная программа) и проверь")
    print("таблицы 'sections' и 'topics' — названия должны быть чистыми, без")
    print("обрезков HTML и значков.")
    print("\nЕсли что-то выглядит неправильно (кривые названия, не найден автор,")
    print("пропущены темы) — пришли мне пример реальной HTML-страницы форума,")
    print("и я поправлю phpbb_parser.py под твою верстку.")

    conn.close()


if __name__ == "__main__":
    main()
