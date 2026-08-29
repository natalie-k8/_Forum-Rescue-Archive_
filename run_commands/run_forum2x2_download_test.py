"""
run_forum2x2_download_test.py — пробный запуск Scanner + Downloader на
РЕАЛЬНОМ форуме forum2x2.ru, в безопасном тестовом режиме (ограниченное
число страниц) — аналог run_download_test.py для первого форума, но с
forum2x2_parser вместо phpbb_parser.

Использует СВОИ отдельные config_test_download_forum2x2.json и
Archive_download_test_forum2x2/ — никак не пересекается с первым форумом.

Вход выполняется, если заполнен .env.forum2x2 — форма входа forum2x2
подтверждена на реальной странице (проще, чем у phpBB: без анти-CSRF
токена). Признак УСПЕШНОГО входа пока не подтверждён на 100% (нет образца
страницы сразу после входа) — см. докстринг login_forum2x2() в
http_client.py. Если после скачивания видно, что закрытые разделы
всё равно не открылись — пришли страницу, которая появляется сразу после
успешного входа, и поправим точный признак.

Запуск:
    py run_forum2x2_download_test.py
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

import parsers.forum2x2_parser as forum2x2_parser
from utils.config import load_config, save_config, ensure_directories
from utils.credentials import load_credentials
from storage.database import connect_db, init_db, QueueManager
from scanner.downloader import Downloader
from network.http_client import ForumClient
from utils.logger_setup import setup_logging
from scanner.rewrite_html import rewrite_all_pages
from scanner.scanner import Scanner

TEST_CONFIG_PATH = "config_test_download_forum2x2.json"
TEST_ARCHIVE_ROOT = "Archive_download_test_forum2x2"


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
    if not url.endswith("/"):
        url += "/"
    return url


def main():
    print("=== Forum Rescue Archive — пробное скачивание с forum2x2 (Scanner + Downloader) ===\n")

    cfg = load_config(TEST_CONFIG_PATH)
    previous_url = cfg.forum.base_url or None

    if previous_url:
        raw_url = input(f"Адрес форума [{previous_url}]: ").strip()
        if not raw_url:
            raw_url = previous_url
    else:
        raw_url = input("Адрес форума forum2x2.ru (главная страница): ").strip()

    if not raw_url:
        print("Адрес форума обязателен. Запусти скрипт заново и введи адрес.")
        sys.exit(1)
    base_url = normalize_base_url(raw_url)

    reset_answer = input(
        "\nНачать заново (удалить результат прошлой пробной попытки)? [Y/n]: "
    ).strip().lower()
    if reset_answer in ("", "y", "yes", "д", "да"):
        # ВАЖНО: раньше здесь стоял ignore_errors=True, который тихо
        # проглатывал ошибку удаления (например, если queue.db-wal ещё
        # занят предыдущим процессом Python — типичная ситуация на
        # Windows после аварийного завершения скрипта) — в итоге
        # сообщение "начинаю с чистого листа" могло быть неправдой:
        # старая база и старые файлы оставались на месте, а Scanner
        # видел их как "уже отсканировано". Теперь ошибка не скрывается.
        try:
            shutil.rmtree(TEST_ARCHIVE_ROOT)
            print("Прошлые результаты удалены — начинаю с чистого листа.")
        except FileNotFoundError:
            print("Прошлых результатов не было — начинаю с чистого листа.")
        except OSError as e:
            print(f"\nНЕ УДАЛОСЬ удалить {TEST_ARCHIVE_ROOT}: {e}")
            print("Похоже, какой-то файл из этой папки открыт в другой программе "
                  "(например, .html открыт в браузере/редакторе, или queue.db открыт "
                  "в программе для просмотра SQLite, или папка открыта в Проводнике). "
                  "Закрой это и запусти скрипт заново — иначе старые записи в базе "
                  "останутся, и Scanner будет считать, что всё уже отсканировано.")
            sys.exit(1)
    else:
        print("Продолжаю с того места, где остановилось прошлое пробное сканирование/скачивание.")

    print("\nЛимиты сканирования (сколько разделов/тем искать структурно):\n")
    max_sections = ask_int("Сколько разделов верхнего уровня сканировать", 2)
    max_topics = ask_int("Не более скольки тем на раздел", 2)
    max_scan_tasks = ask_int("Общий лимит задач при сканировании (страховка)", 20)

    print("\nЛимит скачивания (сколько страниц реально загрузить и сохранить на диск):\n")
    max_download_tasks = ask_int("Сколько страниц скачать в этом пробном прогоне", 5)

    mode_answer = input(
        "\nРежим 2 — скачивать также картинки/аватары/CSS "
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
    logger.info("=== Пробный запуск Scanner + Downloader для forum2x2 (test_mode) ===")

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

    creds = load_credentials(".env.forum2x2", prefix="FORUM2X2")
    do_login = False
    if creds.is_set():
        login_answer = input(
            f"\nНайдены учетные данные в .env.forum2x2 ({creds.username}). "
            "Войти на форум для этого запуска? [y/N]: "
        ).strip().lower()
        do_login = login_answer in ("y", "yes", "д", "да")
    else:
        print("\n.env.forum2x2 не заполнен — вход недоступен, будут видны только открытые разделы.")

    if do_login:
        print(f"Вхожу как {creds.username}...")
        try:
            ok = client.login_forum2x2(creds.username, creds.password)
            print("Логин выполнен успешно." if ok else
                  "Логин НЕ выполнен — проверь логин/пароль в .env.forum2x2.")
            print("(Признак успеха входа для forum2x2 пока не подтверждён на 100% — "
                  "см. докстринг login_forum2x2() в http_client.py. Если после скачивания "
                  "видно, что закрытые разделы всё равно не открылись, а тут написано "
                  "\"успешно\" — пришли мне страницу, которая появляется сразу после входа.)")
        except Exception as e:
            print(f"Ошибка при попытке входа: {e}")
    elif creds.is_set():
        print("Продолжаю без входа — будут доступны только открытые разделы.")

    print(f"\n--- Шаг 1: сканирование структуры ({base_url}) ---")
    print("Каждый запрос выполняется с паузой — это может занять некоторое время.\n")
    scanner = Scanner(cfg, conn, qm, client, parser=forum2x2_parser)
    scan_stats = scanner.run()
    print(f"Разделов найдено: {scan_stats.sections_discovered}, тем найдено: {scan_stats.topics_discovered}")
    print(f"Причина остановки сканирования: {scan_stats.stopped_reason}")

    print(f"\n--- Шаг 2: скачивание текста (лимит {max_download_tasks} страниц) ---\n")
    # credentials передаются Downloader'у, только если реально вошли в этом
    # запуске — иначе при обрыве сессии посреди скачивания он попытался бы
    # войти данными, которые мы сознательно решили не использовать.
    downloader = Downloader(cfg, conn, qm, client, credentials=creds if do_login else None, parser=forum2x2_parser)
    dl_stats = downloader.run(max_tasks=max_download_tasks, wait_for_backoff=False)

    print("\n=== Результат скачивания текста ===")
    print(f"Страниц сохранено:              {dl_stats.pages_saved}")
    print(f"Страниц провалено:               {dl_stats.pages_failed}")
    print(f"Страниц пропущено (404):         {dl_stats.pages_skipped_missing}")
    print(f"Новых страниц тем обнаружено:    {dl_stats.new_topic_pages_discovered}")
    print(f"Ресурсов обнаружено (в очереди): {dl_stats.resources_discovered}")
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
        print("В режиме 2 страницы также должны открываться с картинками/аватарами локально.")
    print(f"\nБаза данных: {cfg.paths.db_path()}")
    print("В таблице 'tasks' можно посмотреть точный статус каждой скачанной страницы.")
    print("\nЕсли что-то выглядит неправильно (кривые названия, не найден автор,")
    print("пропущены темы, не те картинки) — пришли пример реальной страницы, где")
    print("это видно, и поправим forum2x2_parser.py точечно.")

    conn.close()


if __name__ == "__main__":
    main()
