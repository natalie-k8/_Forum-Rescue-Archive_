"""
run_download_test.py — пробный запуск Scanner + Downloader на РЕАЛЬНОМ
форуме, в безопасном тестовом режиме (ограниченное число страниц).

В отличие от run_scan_test.py (который только находит структуру, но
ничего не скачивает), этот скрипт реально сохраняет HTML-страницы на
диск — то есть это первая настоящая проверка архивации, а не только
разведки структуры.

Запуск:
    py run_download_test.py
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
from scanner.downloader import Downloader
from network.http_client import ForumClient
from utils.logger_setup import setup_logging
from scanner.rewrite_html import rewrite_all_pages
from scanner.scanner import Scanner

TEST_CONFIG_PATH = "config_download_test.json"
TEST_ARCHIVE_ROOT = "Archive_download_test"


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
    for suffix in ("index.php/", "index.php"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    if not url.endswith("/"):
        url += "/"
    return url


def main():
    print("=== Forum Rescue Archive — пробное скачивание (Scanner + Downloader) ===\n")

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
        "\nНачать заново (удалить результат прошлой пробной попытки)? [Y/n]: "
    ).strip().lower()
    if reset_answer in ("", "y", "yes", "д", "да"):
        shutil.rmtree(TEST_ARCHIVE_ROOT, ignore_errors=True)
        print("Прошлые результаты удалены — начинаю с чистого листа.")
    else:
        print("Продолжаю с того места, где остановилось прошлое пробное сканирование/скачивание.")

    print("\nЛимиты сканирования (сколько разделов/тем искать структурно):\n")
    max_sections = ask_int("Сколько разделов верхнего уровня сканировать", 5)
    max_topics = ask_int("Не более скольки тем на раздел", 2)
    max_scan_tasks = ask_int("Общий лимит задач при сканировании (страховка)", 30)

    print("\nЛимит скачивания (сколько страниц реально загрузить и сохранить на диск):\n")
    max_download_tasks = ask_int("Сколько страниц скачать в этом пробном прогоне", 10)

    mode_answer = input(
        "\nРежим 2 — скачивать также картинки/вложения/аватары/CSS "
        "(не только текст)? [y/N]: "
    ).strip().lower()
    mode = 2 if mode_answer in ("y", "yes", "д", "да") else 1
    if mode == 2:
        max_resource_tasks = ask_int("Дополнительный лимит на скачивание ресурсов", 10)
    else:
        max_resource_tasks = 0

    cfg.forum.base_url = base_url
    cfg.paths.archive_root = TEST_ARCHIVE_ROOT
    cfg.mode.mode = mode
    cfg.test_mode.enabled = True
    cfg.test_mode.max_sections = max_sections
    cfg.test_mode.max_topics_per_section = max_topics
    cfg.test_mode.max_total_tasks = max_scan_tasks
    save_config(cfg, TEST_CONFIG_PATH)
    ensure_directories(cfg)

    logger = setup_logging(cfg.paths.logs_dir())
    logger.info("=== Пробный запуск Scanner + Downloader (test_mode) ===")

    conn = connect_db(cfg.paths.db_path())
    init_db(conn)
    qm = QueueManager(
        conn,
        backoff_schedule=cfg.backoff.backoff_schedule_seconds,
        reset_streak_after_successes=cfg.backoff.reset_streak_after_successes,
        max_attempts_per_task=cfg.retry.max_attempts_per_task,
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
        print("\nДанные для входа не заданы (.env пуст) — будут доступны только открытые разделы.")

    print(f"\n--- Шаг 1: сканирование структуры ({base_url}) ---")
    print("Каждый запрос выполняется с паузой — это может занять некоторое время.\n")
    scanner = Scanner(cfg, conn, qm, client)
    scan_stats = scanner.run()
    print(f"Разделов найдено: {scan_stats.sections_discovered}, тем найдено: {scan_stats.topics_discovered}")
    print(f"Причина остановки сканирования: {scan_stats.stopped_reason}")

    print(f"\n--- Шаг 2: скачивание текста (лимит {max_download_tasks} страниц) ---\n")
    downloader = Downloader(cfg, conn, qm, client, credentials=creds if creds.is_set() else None)
    dl_stats = downloader.run(max_tasks=max_download_tasks, wait_for_backoff=False)

    print("\n=== Результат скачивания текста ===")
    print(f"Страниц сохранено:              {dl_stats.pages_saved}")
    print(f"Страниц провалено:               {dl_stats.pages_failed}")
    print(f"Страниц пропущено (404):         {dl_stats.pages_skipped_missing}")
    print(f"Новых страниц тем обнаружено:    {dl_stats.new_topic_pages_discovered}")
    print(f"Ресурсов обнаружено (в очереди): {dl_stats.resources_discovered}")
    print(f"Повторных входов при сбросе сессии: {dl_stats.auth_retries}")
    print(f"Причина остановки:               {dl_stats.stopped_reason}")

    if mode == 2:
        print(f"\n--- Шаг 3: скачивание ресурсов (лимит {max_resource_tasks}) ---\n")
        res_stats = downloader.run(max_tasks=max_resource_tasks, wait_for_backoff=False)
        print("\n=== Результат скачивания ресурсов ===")
        print(f"Ресурсов сохранено:  {res_stats.resources_saved}")
        print(f"Ресурсов провалено:   {res_stats.resources_failed}")
        print(f"Ресурсов пропущено:    {res_stats.resources_skipped_missing}")
        print(f"Причина остановки:      {res_stats.stopped_reason}")

        print("\n--- Шаг 4: переписывание HTML на локальные пути ---\n")
        rewrite_stats = rewrite_all_pages(cfg, conn)
        print(f"Страниц переписано: {rewrite_stats.pages_rewritten}, "
              f"ссылок заменено: {rewrite_stats.links_replaced}")

    print(f"\nСкачанные файлы лежат в: {cfg.paths.archive_root}/forum/...")
    print("Открой пару .html файлов в браузере — так проще всего проверить, что")
    print("страницы сохранились полностью и текст сообщений на месте.")
    if mode == 2:
        print("В режиме 2 страницы также должны открываться с оформлением "
              "(картинки и стили загружены локально).")
    print(f"\nБаза данных: {cfg.paths.db_path()}")
    print("В таблице 'tasks' можно посмотреть точный статус каждой скачанной страницы.")

    conn.close()


if __name__ == "__main__":
    main()
