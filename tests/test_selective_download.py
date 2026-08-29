"""
test_selective_download.py — проверка выборочной архивации по разделам.

Пользователь хочет иметь возможность скачать только выбранные важные
разделы, а не весь форум сразу (форум большой, ~59 000 сообщений, и
времени на полный прогон может не хватить). Scanner работает как
раньше — находит всю структуру и ставит всё в очередь; ограничение
применяется только на этапе Downloader.run(allowed_section_ids=[...]):
    - section_page/topic_page скачиваются только если их section_id в
      списке (Task.section_id, заполняется Scanner при постановке);
    - ресурсы (картинки/CSS/...) не имеют своего section_id — они
      скачиваются, если их родительская текстовая задача (parent_id)
      принадлежит выбранному разделу;
    - задачи невыбранных разделов остаются в очереди нетронутыми —
      ничего не удаляется, не помечается failed/skipped, просто не
      берётся в этом прогоне.
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import http.server
import shutil
import threading
from urllib.parse import urlparse, parse_qs

import os

from utils.config import AppConfig, ensure_directories
from storage.database import (
    connect_db, init_db, QueueManager,
    TYPE_SECTION_PAGE, TYPE_TOPIC_PAGE, TYPE_IMAGE,
)
from scanner.downloader import Downloader
from network.http_client import ForumClient

TEST_ROOT = "Archive_selective_download_test"

SECTION_A_HTML = """<html><body>
<p>Раздел A</p>
<img src="./images/a_photo.jpg"/>
</body></html>"""

SECTION_B_HTML = """<html><body>
<p>Раздел B</p>
<img src="./images/b_photo.jpg"/>
</body></html>"""


class MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/viewforum.php" and qs.get("f") == ["1"]:
            self._html(200, SECTION_A_HTML)
        elif parsed.path == "/viewforum.php" and qs.get("f") == ["2"]:
            self._html(200, SECTION_B_HTML)
        elif parsed.path in ("/images/a_photo.jpg", "/images/b_photo.jpg"):
            self._binary(200, b"\xff\xd8\xff\xe0fake-jpeg-bytes", "image/jpeg")
        else:
            self._html(404, "not found")

    def _html(self, code, html):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _binary(self, code, content, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(content)


def start_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def main():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    server, port = start_server()
    base_url = f"http://127.0.0.1:{port}/"

    cfg = AppConfig()
    cfg.paths.archive_root = TEST_ROOT
    cfg.forum.base_url = base_url
    cfg.throttle.min_delay_seconds = 0.001
    cfg.throttle.max_delay_seconds = 0.002
    cfg.mode.mode = 2
    ensure_directories(cfg)

    conn = connect_db(cfg.paths.db_path())
    init_db(conn)
    qm = QueueManager(conn, backoff_schedule=[0], reset_streak_after_successes=3,
                       max_attempts_per_task=1)
    client = ForumClient(base_url, cfg.forum.user_agent,
                          cfg.throttle.min_delay_seconds, cfg.throttle.max_delay_seconds)

    conn.execute(
        "INSERT INTO sections (id, forum_id, name, url, parent_id, page_count, scanned) "
        "VALUES ('A', 'A', 'РазделA', ?, NULL, 1, 1)",
        (base_url + "viewforum.php?f=1",),
    )
    conn.execute(
        "INSERT INTO sections (id, forum_id, name, url, parent_id, page_count, scanned) "
        "VALUES ('B', 'B', 'РазделB', ?, NULL, 1, 1)",
        (base_url + "viewforum.php?f=2",),
    )
    conn.commit()

    # Как это сделал бы Scanner: section_id заполнен при постановке задачи.
    qm.add_task(TYPE_SECTION_PAGE, base_url + "viewforum.php?f=1",
                "forum/sections/РазделA/page_001.html", section_id="A")
    qm.add_task(TYPE_SECTION_PAGE, base_url + "viewforum.php?f=2",
                "forum/sections/РазделB/page_001.html", section_id="B")

    downloader = Downloader(cfg, conn, qm, client)

    # --- Скачиваем только раздел A ---
    stats = downloader.run(wait_for_backoff=False, allowed_section_ids=["A"])

    assert stats.pages_saved == 1, f"Ожидалась 1 страница (раздел A), получено {stats.pages_saved}"
    print("[OK] При allowed_section_ids=['A'] скачана только 1 страница (раздел A)")

    # Раздел B должен остаться нетронутым (pending), не failed/skipped.
    row_b = conn.execute(
        "SELECT status FROM tasks WHERE section_id = 'B'"
    ).fetchone()
    assert row_b["status"] == "pending", (
        f"Раздел B не выбран в этом прогоне — должен остаться pending, получено {row_b['status']}"
    )
    print("[OK] Невыбранный раздел B остался нетронутым в очереди (status=pending)")

    # Ресурс со страницы раздела A должен был скачаться (наследует раздел от parent_id).
    resource_a = conn.execute(
        "SELECT status FROM tasks WHERE type = ? AND url LIKE '%a_photo.jpg%'", (TYPE_IMAGE,)
    ).fetchone()
    assert resource_a is not None and resource_a["status"] == "done", (
        f"Картинка со страницы раздела A должна была скачаться, получено: {resource_a}"
    )
    print("[OK] Ресурс (картинка) со страницы выбранного раздела A скачан")

    # --- Теперь скачиваем раздел B тоже: новый Downloader, как при
    # реальном перезапуске программы (DownloadStats не накапливается
    # между отдельными запусками py run_archive.py) ---
    downloader2 = Downloader(cfg, conn, qm, client)
    stats2 = downloader2.run(wait_for_backoff=False, allowed_section_ids=["B"])
    assert stats2.pages_saved == 1, f"Ожидалась 1 страница (раздел B), получено {stats2.pages_saved}"
    row_b_after = conn.execute("SELECT status FROM tasks WHERE section_id = 'B'").fetchone()
    assert row_b_after["status"] == "done", "Раздел B должен был скачаться во втором прогоне"
    print("[OK] Раздел B скачан отдельным прогоном с allowed_section_ids=['B'] — раздел A не пострадал")

    # --- Без фильтра (allowed_section_ids=None) — поведение как раньше, всё доступное берётся ---
    row_a_check = conn.execute("SELECT status FROM tasks WHERE section_id = 'A'").fetchone()
    assert row_a_check["status"] == "done", "Раздел A должен остаться done после прогона без него в списке"
    print("[OK] Прошлый выбор не откатывается — раздел A остаётся done")

    conn.close()
    server.shutdown()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)

    print("\nВСЕ ПРОВЕРКИ выборочной архивации по разделам ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
