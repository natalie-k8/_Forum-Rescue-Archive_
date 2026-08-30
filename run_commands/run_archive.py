"""
run_archive.py — БОЕВОЙ (не тестовый) запуск архивации форума.

В отличие от run_download_test.py (тестовый режим с искусственными
ограничениями на число разделов/тем — для быстрой проверки, что всё
работает), этот скрипт:
  - сканирует ВЕСЬ форум целиком (если явно не ограничить сканирование
    отдельным вопросом при запуске);
  - НИЧЕГО не удаляет — всегда продолжает с того места, где остановился
    (это боевой архив, тут не должно быть кнопки "стереть всё" по ошибке);
  - поддерживает НЕСКОЛЬКО форумов на разных хостах (через
    utils/site_registry.py) — при первом запуске существующие
    config.json/Archive/.env заносятся в реестр как есть (ничего не
    переименовывается), любой новый адрес получает свои файлы
    автоматически, не пересекаясь с уже архивируемыми форумами;
  - при сбое сервера/паузе (backoff) ЖДЁТ и продолжает сам, а не
    останавливается сразу — рассчитан на то, что будет работать долго
    (возможно, часы) без присмотра. Прервать в любой момент можно
    обычным Ctrl+C — прогресс не потеряется, при следующем запуске
    продолжит с того же места.

Запуск:
    py run_archive.py
"""


from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше run_commands/) — нужен, чтобы
# "from utils...." и подобные внутрипроектные импорты работали
# независимо от того, откуда запущен этот файл (через меню main.py,
# или напрямую: py run_commands/run_archive.py).


import sys
from urllib.parse import urlparse

from utils.config import load_config, save_config, ensure_directories
from utils.credentials import load_credentials
from utils.site_registry import get_or_create_site, list_sites, seed_legacy_site
from storage.database import connect_db, init_db, QueueManager
from scanner.downloader import Downloader
from network.http_client import ForumClient
from utils.logger_setup import setup_logging
from scanner.rewrite_html import rewrite_all_pages
from scanner.scanner import Scanner

SITE_REGISTRY_PATH = "sites_phpbb.json"
CONFIG_PREFIX = "config_phpbb"
ARCHIVE_PREFIX = "Archive_phpbb"
LEGACY_CONFIG_PATH = "config.json"


def choose_sections_to_download(conn) -> list[str] | None:
    """
    Показывает список ВСЕХ известных разделов (из уже отсканированной
    структуры, включая прошлые прогоны) и даёт выбрать, какие качать в
    этом запуске. Возвращает список выбранных section_id, или None —
    если пользователь выбрал "всё" (поведение как раньше, без фильтра).

    Не трогает Scanner и саму структуру — только ограничивает, что
    Downloader возьмёт из уже поставленной в очередь работы. Разделы,
    не выбранные сейчас, остаются нетронутыми в очереди для следующего
    прогона — ничего не удаляется и не помечается как пропущенное.
    """
    sections = conn.execute(
        """
        SELECT s.id, s.name, s.parent_id,
               (SELECT COUNT(*) FROM topics t WHERE t.section_id = s.id) as topic_count
        FROM sections s
        ORDER BY s.rowid ASC
        """
    ).fetchall()

    if not sections:
        return None  # структура ещё не известна (самый первый запуск) — качаем всё, что найдётся

    print("\nИзвестные разделы (из уже отсканированной структуры):\n")
    for i, s in enumerate(sections, start=1):
        indent = "  ↳ " if s["parent_id"] else "  "
        print(f"  {i}. {indent}{s['name']}  (тем известно: {s['topic_count']})")

    print(
        "\nСкачать всё (пусто), или только выбранные разделы? "
        "Через запятую номера нужных разделов (например: 1,3,5): "
    )
    choice = input().strip()
    if not choice:
        return None

    indices = []
    for part in choice.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(sections):
            indices.append(int(part) - 1)
        elif part:
            print(f"Пропущено нераспознанное значение: '{part}'")

    if not indices:
        print("Ни один раздел не распознан — скачивается всё.")
        return None

    selected_ids = [sections[i]["id"] for i in indices]
    selected_names = [sections[i]["name"] for i in indices]
    print(f"Выбрано разделов: {len(selected_ids)} — {', '.join(selected_names)}")
    return selected_ids
    
LEGACY_ARCHIVE_ROOT = "Archive"
LEGACY_CREDENTIALS_PATH = ".env"


def ask_int_or_none(prompt: str) -> int | None:
    raw = input(f"{prompt} [пусто = без ограничения]: ").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    print("Не похоже на число — считаю, что без ограничения.")
    return None


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


def choose_site() -> tuple[str, str, str, str]:
    """
    Показывает список уже известных форумов (каждый — со своим адресом,
    config_path, archive_root и своим .env для логина) и даёт выбрать
    один из них либо добавить новый адрес. Возвращает (base_url,
    config_path, archive_root, credentials_path).

    При самом первом запуске после этой доработки реестр пуст, а
    config.json/Archive/.env уже могут быть в реальной работе (это же
    боевой архив) — seed_legacy_site() один раз заносит их в реестр КАК
    ЕСТЬ, под их текущими именами, ничего не переименовывая на диске.
    """
    seed_legacy_site(SITE_REGISTRY_PATH, LEGACY_CONFIG_PATH, LEGACY_ARCHIVE_ROOT, LEGACY_CREDENTIALS_PATH)
    sites = list_sites(SITE_REGISTRY_PATH)

    if sites:
        print("Уже известные форумы:\n")
        for i, site in enumerate(sites, start=1):
            print(f"  {i}. {site.base_url}  →  {site.archive_root}/  "
                  f"(логин: {site.credentials_path})")
        print(f"  0. Новый форум (другой адрес)\n")

        choice = input("Выбери номер или 0 для нового форума: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(sites):
            site = sites[int(choice) - 1]
            return site.base_url, site.config_path, site.archive_root, site.credentials_path
        print()

    raw_url = input("Адрес форума (например, http://myforum.ru/): ").strip()
    if not raw_url:
        print("Адрес форума обязателен. Запусти скрипт заново и введи адрес.")
        sys.exit(1)
    base_url = normalize_base_url(raw_url)

    site, is_new = get_or_create_site(
        SITE_REGISTRY_PATH, base_url, CONFIG_PREFIX, ARCHIVE_PREFIX,
        credentials_prefix=".env",
    )
    if is_new:
        print(f"\nЭто новый форум — под него автоматически заведены свои "
              f"config ({site.config_path}), папка архива ({site.archive_root}/) "
              f"и свой файл для логина ({site.credentials_path} — скопируй в него "
              f".env.example и впиши данные для ЭТОГО форума).")
    return site.base_url, site.config_path, site.archive_root, site.credentials_path


def main():
    print("=== Forum Rescue Archive — БОЕВОЙ запуск архивации ===\n")

    base_url, config_path, archive_root, credentials_path = choose_site()
    cfg = load_config(config_path)

    mode_answer = input(
        "\nРежим 2 — скачивать также картинки/вложения/аватары/CSS "
        "(не только текст)? [Y/n]: "
    ).strip().lower()
    mode = 1 if mode_answer in ("n", "no", "н", "нет") else 2

    print(
        "\nПо умолчанию сканирование проходит ВЕСЬ форум целиком (все "
        "разделы, все страницы списков тем) — на большом форуме это может "
        "занять очень долго (часы), ДО того, как вообще начнётся "
        "скачивание. Лимиты страниц/ресурсов ниже на само сканирование "
        "не влияют — они только про то, сколько уже НАЙДЕННОГО скачать."
    )
    limit_scan_answer = input(
        "Ограничить и само сканирование в этом запуске (например, чтобы "
        "сначала попробовать на паре разделов)? [y/N]: "
    ).strip().lower()
    if limit_scan_answer in ("y", "yes", "д", "да"):
        max_scan_sections = ask_int_or_none("Сколько разделов верхнего уровня сканировать")
        max_scan_topics = ask_int_or_none("Не более скольки тем на раздел")
        cfg.test_mode.enabled = True
        cfg.test_mode.max_sections = max_scan_sections
        cfg.test_mode.max_topics_per_section = max_scan_topics
        cfg.test_mode.max_total_tasks = None
        cfg.test_mode.max_pages_per_topic = None
    else:
        cfg.test_mode.enabled = False

    print(
        "\nСколько страниц/ресурсов обрабатывать за этот запуск. Можно "
        "оставить пустым — тогда будет работать, пока не закончит весь "
        "форум или пока не прервёшь через Ctrl+C (потом можно продолжить "
        "новым запуском этого же скрипта)."
    )
    max_download_tasks = ask_int_or_none("Лимит страниц текста в этом запуске")
    max_resource_tasks = ask_int_or_none("Лимит ресурсов в этом запуске") if mode == 2 else 0

    cfg.forum.base_url = base_url
    cfg.paths.archive_root = archive_root
    cfg.mode.mode = mode
    save_config(cfg, config_path)
    ensure_directories(cfg)

    logger = setup_logging(cfg.paths.logs_dir())
    logger.info("=== Боевой запуск архивации форума ===")

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

    creds = load_credentials(credentials_path)
    do_login = False
    if creds.is_set():
        login_answer = input(f"\nНайдены учетные данные в {credentials_path} ({creds.username}). "
                              "Войти на форум? [Y/n]: ").strip().lower()
        do_login = login_answer not in ("n", "no", "н", "нет")
    else:
        print(f"\nДанные для входа не заданы ({credentials_path} пуст) — будут доступны только открытые разделы.")

    if do_login:
        print(f"Вхожу как {creds.username}...")
        try:
            ok = client.login(creds.username, creds.password)
            print("Логин выполнен успешно." if ok else f"Логин НЕ выполнен — проверь логин/пароль в {credentials_path}.")
        except Exception as e:
            print(f"Ошибка при попытке входа: {e}")
    elif creds.is_set():
        print("Продолжаю без входа — будут доступны только открытые разделы.")

    print(f"\n--- Сканирование структуры ({base_url}) ---")
    print("Каждый запрос выполняется с паузой — сканирование всего форума может занять долго.\n")
    try:
        scanner = Scanner(cfg, conn, qm, client)
        scan_stats = scanner.run()
        print(f"Разделов найдено: {scan_stats.sections_discovered}, тем найдено: {scan_stats.topics_discovered}")
        print(f"Причина остановки сканирования: {scan_stats.stopped_reason}")

        allowed_section_ids = choose_sections_to_download(conn)

        print(f"\n--- Скачивание текста ---\n")
        downloader = Downloader(cfg, conn, qm, client, credentials=creds if do_login else None)
        dl_stats = downloader.run(max_tasks=max_download_tasks, wait_for_backoff=True,
                                   allowed_section_ids=allowed_section_ids)

        print("\n=== Результат скачивания текста ===")
        print(f"Страниц сохранено:              {dl_stats.pages_saved}")
        print(f"Страниц провалено:               {dl_stats.pages_failed}")
        print(f"Страниц пропущено (404):         {dl_stats.pages_skipped_missing}")
        print(f"Новых страниц тем обнаружено:    {dl_stats.new_topic_pages_discovered}")
        print(f"Ресурсов обнаружено (в очереди): {dl_stats.resources_discovered}")
        print(f"Причина остановки:               {dl_stats.stopped_reason}")

        if mode == 1 and dl_stats.resources_deferred_by_mode:
            print(
                f"\nВНИМАНИЕ: {dl_stats.resources_deferred_by_mode} ресурсов "
                "(картинки/вложения/аватары/CSS-оформление) НЕ скачаны, "
                "потому что архив шёл в режиме 1 \"Спасение текста\". Они уже "
                "стоят в очереди — чтобы скачать их, переключи режим на 2 "
                "\"Полный архив\" и запусти скрипт снова (текст пересканировать "
                "не придётся)."
            )

        if mode == 2:
            print(f"\n--- Скачивание ресурсов ---\n")
            try:
                res_stats = downloader.run(max_tasks=max_resource_tasks, wait_for_backoff=True,
                                            allowed_section_ids=allowed_section_ids)
                print("\n=== Результат скачивания ресурсов ===")
                print(f"Ресурсов сохранено:  {res_stats.resources_saved}")
                print(f"Ресурсов провалено:   {res_stats.resources_failed}")
                print(f"Ресурсов пропущено:    {res_stats.resources_skipped_missing}")
                print(f"Причина остановки:      {res_stats.stopped_reason}")
            except KeyboardInterrupt:
                print("\n\nОстановлено (Ctrl+C) во время скачивания ресурсов — "
                      "переписываю уже скачанное перед выходом...")

            print("\n--- Переписывание HTML на локальные пути ---\n")
            rewrite_stats = rewrite_all_pages(cfg, conn)
            print(f"Страниц переписано: {rewrite_stats.pages_rewritten}, "
                  f"ссылок заменено: {rewrite_stats.links_replaced}")
    except KeyboardInterrupt:
        print("\n\nОстановлено (Ctrl+C) — прогресс сохранён, запусти скрипт снова, чтобы продолжить.")
    finally:
        conn.close()

    print(f"\nАрхив: {cfg.paths.archive_root}/forum/...")
    print(f"База данных: {cfg.paths.db_path()}")


if __name__ == "__main__":
    main()
