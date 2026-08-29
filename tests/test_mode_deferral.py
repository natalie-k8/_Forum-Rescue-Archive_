"""
test_mode_deferral.py — проверка учёта ресурсов, отложенных режимом 1
"Спасение текста" (priority_ceiling=1): CSS/картинки/аватары/смайлики
ставятся в очередь, но не скачиваются, пока режим не переключат на 2.

Регрессионный тест для найденного при разборе CSS-проблемы поведения:
QueueManager.count_deferred_by_mode() и Downloader.run() должны честно
сообщать, сколько ресурсов ждёт своей очереди из-за режима — отдельно
от ошибок/backoff, чтобы пользователь не путал "не скачалось из-за сбоя"
с "не скачалось, потому что режим 1".
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import http.server
import shutil
import threading
from urllib.parse import urlparse, parse_qs

from utils.config import AppConfig, ensure_directories
from storage.database import (
    connect_db, init_db, QueueManager,
    TYPE_SECTION_PAGE, TYPE_CSS, TYPE_IMAGE,
    PRIORITY_TEXT, PRIORITY_RESOURCE,
)
from scanner.downloader import Downloader
from network.http_client import ForumClient

TEST_ROOT = "Archive_mode_deferral_test"

SECTION_HTML = """<html><head>
<link rel="stylesheet" href="./styles/theme.css"/>
</head><body>
<p>Список тем раздела</p>
<img src="./images/photo.jpg"/>
</body></html>"""


class MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/viewforum.php":
            self._html(200, SECTION_HTML)
        else:
            self._html(404, "not found")

    def _html(self, code, html):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


def start_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def test_count_deferred_by_mode_queue():
    """QueueManager.count_deferred_by_mode: считает только незавершённые
    задачи с приоритетом выше текущего потолка режима."""
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    cfg = AppConfig()
    cfg.paths.archive_root = TEST_ROOT
    ensure_directories(cfg)

    conn = connect_db(cfg.paths.db_path())
    init_db(conn)
    qm = QueueManager(conn, backoff_schedule=[0], reset_streak_after_successes=3,
                       max_attempts_per_task=20)

    text_task = qm.add_task(TYPE_SECTION_PAGE, "http://forum.test/viewforum.php?f=1",
                             "forum/sections/A/page_001.html")
    css_task = qm.add_task(TYPE_CSS, "http://forum.test/styles/theme.css", "css/theme.css")
    img_task = qm.add_task(TYPE_IMAGE, "http://forum.test/images/photo.jpg", "images/photo.jpg")

    # В режиме 1 (priority_ceiling=1) оба ресурса (priority=2) отложены,
    # текстовая задача (priority=1) — нет.
    deferred = qm.count_deferred_by_mode(priority_ceiling=PRIORITY_TEXT)
    assert deferred == 2, f"Ожидалось 2 отложенных ресурса, получено {deferred}"
    print("[OK] count_deferred_by_mode считает CSS и картинку как отложенные в режиме 1")

    # В режиме 2 (priority_ceiling=2) ничего не отложено режимом.
    deferred_mode2 = qm.count_deferred_by_mode(priority_ceiling=PRIORITY_RESOURCE)
    assert deferred_mode2 == 0, f"В режиме 2 ничего не должно быть отложено, получено {deferred_mode2}"
    print("[OK] count_deferred_by_mode возвращает 0 в режиме 2 (все приоритеты доступны)")

    # Если ресурс уже скачан (done), он больше не "отложен".
    qm.mark_task_done(css_task)
    deferred_after_done = qm.count_deferred_by_mode(priority_ceiling=PRIORITY_TEXT)
    assert deferred_after_done == 1, f"Ожидался 1 (только картинка), получено {deferred_after_done}"
    print("[OK] Уже скачанный ресурс (status=done) не считается отложенным")

    conn.close()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def test_downloader_reports_deferred_resources():
    """Downloader.run() в режиме 1: страница скачивается, найденные на ней
    CSS/картинка ставятся в очередь, но не скачиваются — стата
    resources_deferred_by_mode должна отразить это по итогам run()."""
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    server, port = start_server()
    base_url = f"http://127.0.0.1:{port}/"

    cfg = AppConfig()
    cfg.paths.archive_root = TEST_ROOT
    cfg.forum.base_url = base_url
    cfg.throttle.min_delay_seconds = 0.001
    cfg.throttle.max_delay_seconds = 0.002
    cfg.mode.mode = 1  # "Спасение текста"
    ensure_directories(cfg)

    conn = connect_db(cfg.paths.db_path())
    init_db(conn)
    qm = QueueManager(conn, backoff_schedule=[0], reset_streak_after_successes=3,
                       max_attempts_per_task=1)
    client = ForumClient(base_url, cfg.forum.user_agent,
                          cfg.throttle.min_delay_seconds, cfg.throttle.max_delay_seconds)

    qm.add_task(TYPE_SECTION_PAGE, base_url + "viewforum.php?f=1",
                "forum/sections/A/page_001.html")

    downloader = Downloader(cfg, conn, qm, client)
    stats = downloader.run(wait_for_backoff=False)

    assert stats.pages_saved == 1, f"Ожидалась 1 сохранённая страница, получено {stats.pages_saved}"
    assert stats.resources_discovered == 2, (
        f"Ожидалось 2 обнаруженных ресурса (CSS + картинка), получено {stats.resources_discovered}"
    )
    assert stats.resources_deferred_by_mode == 2, (
        f"Ожидалось 2 отложенных режимом ресурса, получено {stats.resources_deferred_by_mode}"
    )
    print("[OK] Downloader.run() в режиме 1 честно сообщает 2 ресурса, отложенных режимом (CSS + картинка)")

    conn.close()
    server.shutdown()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def main():
    test_count_deferred_by_mode_queue()
    test_downloader_reports_deferred_resources()
    print("\nВСЕ ПРОВЕРКИ учёта ресурсов, отложенных режимом, ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
