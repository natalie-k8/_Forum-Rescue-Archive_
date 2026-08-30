"""
test_backfill_css_imports.py — проверка Downloader.backfill_css_imports():
доначинка очереди для CSS-файлов, скачанных ДО того, как программа
научилась разбирать @import внутри уже скачанного CSS (см.
tests/test_css_imports.py — тот тест проверяет поведение для СВЕЖЕГО
скачивания; этот — для случая, когда style.css уже был status='done'
раньше и никогда не парсился на @import).

Ключевая проверка: backfill НЕ делает сетевых запросов (сервер в этом
тесте не поднимается вообще) — работает только с тем, что уже есть на
диске и в БД.
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import shutil

from utils.config import AppConfig, ensure_directories
from storage.database import connect_db, init_db, QueueManager, TYPE_CSS
from scanner.downloader import Downloader, save_bytes_atomic
from network.http_client import ForumClient

TEST_ROOT = "Archive_backfill_css_test"
BASE_URL = "http://forum.test/"

# Имитация "старого" style.css, скачанного до появления @import-фикса —
# уже лежит на диске и помечен done, но normalize.css/colours.css из
# его @import никогда не были поставлены в очередь.
STYLE_CSS = """
@import url("normalize.css?v=3.2");
@import url("colours.css?v=3.2");
"""


def main():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)

    cfg = AppConfig()
    cfg.paths.archive_root = TEST_ROOT
    cfg.forum.base_url = BASE_URL
    ensure_directories(cfg)

    conn = connect_db(cfg.paths.db_path())
    init_db(conn)
    qm = QueueManager(conn, cfg.backoff.backoff_schedule_seconds,
                       cfg.backoff.reset_streak_after_successes,
                       cfg.retry.max_attempts_per_task)

    # Имитация состояния "до фикса": style.css уже done, его @import
    # никогда не разбирался.
    style_task_id = qm.add_task(TYPE_CSS, BASE_URL + "styles/style.css", "css/abc_style.css")
    qm.mark_task_done(style_task_id)
    save_bytes_atomic(os.path.join(TEST_ROOT, "css/abc_style.css"), STYLE_CSS.encode("utf-8"))

    # Клиент не должен использоваться вообще — намеренно указываем на
    # несуществующий адрес, чтобы любой реальный сетевой запрос упал
    # с ошибкой соединения и тест заметил бы это как исключение.
    client = ForumClient("http://127.0.0.1:1/", cfg.forum.user_agent,
                          cfg.throttle.min_delay_seconds, cfg.throttle.max_delay_seconds)
    downloader = Downloader(cfg, conn, qm, client)

    css_before = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE type = ?", (TYPE_CSS,)).fetchone()["n"]
    assert css_before == 1, "До backfill в очереди должен быть только сам style.css"

    new_count = downloader.backfill_css_imports()
    assert new_count == 2, f"Ожидалось 2 новые ссылки (normalize.css, colours.css), получено {new_count}"
    print(f"[OK] backfill_css_imports нашёл и поставил в очередь {new_count} новых ссылок")

    css_tasks = conn.execute("SELECT * FROM tasks WHERE type = ?", (TYPE_CSS,)).fetchall()
    css_urls = {row["url"] for row in css_tasks}
    assert any(u.startswith(BASE_URL + "styles/normalize.css") for u in css_urls)
    assert any(u.startswith(BASE_URL + "styles/colours.css") for u in css_urls)
    print("[OK] normalize.css и colours.css появились в очереди как pending-задачи")

    new_tasks = [row for row in css_tasks if row["url"] != BASE_URL + "styles/style.css"]
    assert all(row["status"] == "pending" for row in new_tasks), \
        "Новые задачи должны быть pending — backfill только ставит в очередь, не скачивает"
    print("[OK] Новые задачи в статусе pending — backfill не делает сетевых запросов сам")

    # Идемпотентность: повторный вызов не находит уже известные URL заново.
    second_count = downloader.backfill_css_imports()
    assert second_count == 0, f"Повторный вызов не должен находить новых ссылок, получено {second_count}"
    print("[OK] Повторный вызов backfill_css_imports идемпотентен (0 новых ссылок)")

    conn.close()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)

    print("\nВСЕ ПРОВЕРКИ backfill_css_imports ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
