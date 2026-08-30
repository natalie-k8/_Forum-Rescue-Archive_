"""
backfill_css_imports.py — доначинка очереди для CSS-файлов, скачанных ДО
того, как Downloader научился разбирать @import/url(...) ВНУТРИ уже
скачанного CSS (см. scanner/css_links.py, Downloader._enqueue_css_imports).

Контекст: типичный phpBB style.css сам почти пустой и подключает другие
файлы (normalize.css, base.css, colours.css, ...) через @import. Этот
разбор происходит автоматически, но только В МОМЕНТ, когда CSS-файл
скачивается ЗАНОВО — если style.css был скачан РАНЬШЕ (до обновления
программы), он уже помечен status='done' и не будет перескачан сам по
себе, а значит его @import никогда не разберётся без этого шага.

Что делает:
    - НЕ обращается к форуму по сети вообще — не нагружает сервер;
    - читает с диска уже скачанные CSS-файлы (status='done' в очереди);
    - находит в них @import/url(...) на другие файлы;
    - доставляет найденные ссылки в очередь как обычные задачи — сами
      файлы скачает следующий обычный запуск run_archive.py /
      run_forum2x2_archive.py (фаза "Скачивание ресурсов"), как всегда.

Безопасно запускать сколько угодно раз (идемпотентно) — уже известные
URL повторно не добавляются в очередь.

Запуск:
    py backfill_css_imports.py
"""


from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше run_commands/) — нужен, чтобы
# "from utils...." и подобные внутрипроектные импорты работали
# независимо от того, откуда запущен этот файл (через меню main.py,
# или напрямую: py run_commands/backfill_css_imports.py).


import os
from urllib.parse import urlparse

from utils.config import load_config
from utils.site_registry import list_sites
from storage.database import connect_db, init_db, QueueManager, TYPE_CSS, STATUS_DONE
from scanner.downloader import Downloader
from network.http_client import ForumClient

# Оба реестра — форум №1 (phpBB) и forum2x2 — используют один и тот же
# формат (utils/site_registry.py): "адрес форума -> свой config_path/
# archive_root/credentials_path". Раньше форум №1 был жёстко привязан к
# единственному config.json — если где-то ещё остался такой архив без
# реестра (LEGACY_CONFIG_PATH), он тоже предлагается в списке.
PHPBB_SITE_REGISTRY_PATH = "sites_phpbb.json"
FORUM2X2_SITE_REGISTRY_PATH = "sites_forum2x2.json"
LEGACY_CONFIG_PATH = "config.json"


def choose_config_path() -> str | None:
    """
    Спрашивает, для какого форума делать доначинку — собирает варианты из
    обоих реестров сайтов (phpBB и forum2x2) плюс старый config.json, если
    он ещё существует и не был перенесён в реестр. Возвращает путь к
    config.json выбранного форума, либо None, если пользователь отменил
    выбор.
    """
    options: list[tuple[str, str]] = []  # (подпись для печати, config_path)

    for site in list_sites(PHPBB_SITE_REGISTRY_PATH):
        options.append((f"{site.base_url}  (форум №1, {site.config_path})", site.config_path))
    for site in list_sites(FORUM2X2_SITE_REGISTRY_PATH):
        options.append((f"{site.base_url}  (forum2x2, {site.config_path})", site.config_path))
    if os.path.exists(LEGACY_CONFIG_PATH):
        options.append((f"{LEGACY_CONFIG_PATH} (старый формат, без реестра)", LEGACY_CONFIG_PATH))

    if not options:
        print("Не найдено ни одного известного форума (нет ни sites_phpbb.json, "
              "ни sites_forum2x2.json, ни config.json) — запусти обычную "
              "архивацию сначала.")
        return None

    print("Для какого форума проверить уже скачанные CSS-файлы?\n")
    for i, (label, _) in enumerate(options, start=1):
        print(f"  {i}. {label}")
    print("  0. Отмена")

    choice = input("\nВыбери номер: ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(options):
        return options[int(choice) - 1][1]
    return None


def main():
    print("=== Forum Rescue Archive — доначинка @import внутри CSS ===\n")

    config_path = choose_config_path()
    if not config_path:
        print("Отменено.")
        return

    cfg = load_config(config_path)
    if not cfg.forum.base_url:
        print(f"В {config_path} не задан адрес форума — нечего проверять.")
        return

    conn = connect_db(cfg.paths.db_path())
    init_db(conn)

    already_css = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE status = ? AND type = ?",
        (STATUS_DONE, TYPE_CSS),
    ).fetchone()["n"]
    print(f"\nУже скачанных CSS-файлов в очереди: {already_css}")

    if already_css == 0:
        print("Нечего проверять — ни один CSS-файл ещё не скачан. "
              "Запусти обычную архивацию сначала.")
        conn.close()
        return

    qm = QueueManager(
        conn,
        backoff_schedule=cfg.backoff.backoff_schedule_seconds,
        reset_streak_after_successes=cfg.backoff.reset_streak_after_successes,
        max_attempts_per_task=cfg.retry.max_attempts_per_task,
        max_attempts_per_task_external=cfg.retry.max_attempts_per_task_external,
        own_host=urlparse(cfg.forum.base_url).netloc,
    )
    # Клиент нужен только для сигнатуры Downloader — сетевых запросов в
    # этом скрипте не будет (backfill_css_imports читает файлы с диска).
    client = ForumClient(
        cfg.forum.base_url, cfg.forum.user_agent,
        cfg.throttle.min_delay_seconds, cfg.throttle.max_delay_seconds,
    )

    downloader = Downloader(cfg, conn, qm, client)
    print("Читаю уже скачанные CSS-файлы и ищу @import/url(...) на другие файлы...")
    new_tasks = downloader.backfill_css_imports()

    print(f"\nНайдено и поставлено в очередь новых ссылок: {new_tasks}")
    if new_tasks:
        print("Запусти обычную боевую архивацию снова (пункт «Скачивание ресурсов») — "
              "новые CSS-файлы скачаются как обычные ресурсы, и оформление "
              "должно подтянуться после следующего переписывания HTML.")
    else:
        print("Новых ссылок не найдено — либо все уже были обнаружены раньше, "
              "либо в этих CSS-файлах нет @import/url(...) на другие файлы.")

    conn.close()


if __name__ == "__main__":
    main()