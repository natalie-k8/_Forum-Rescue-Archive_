"""
test_downloader.py — проверка Downloader на локальном мок-сервере:
    - сохранение HTML (оригинал + чистовик) на диск;
    - инкрементальное обнаружение страниц 2, 3 многостраничной темы;
    - корректная обработка 404 (skipped_missing) и 502 (failed/backoff);
    - автоматический повторный вход при истекшей сессии (auth_required).
"""


import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше tests/) — нужен, чтобы "from utils...."
# и подобные внутрипроектные импорты работали независимо от того, откуда
# запущен этот файл (py tests/test_x.py из корня, или py test_x.py изнутри tests/).

import http.server
import shutil
import threading
from urllib.parse import urlparse, parse_qs

from utils.config import AppConfig, ensure_directories
from utils.credentials import ForumCredentials
from storage.database import (
    connect_db, init_db, QueueManager, TYPE_SECTION_PAGE, TYPE_TOPIC_PAGE,
    STATUS_SKIPPED_MISSING,
)
from scanner.downloader import Downloader
from network.http_client import ForumClient

TEST_ROOT = "Archive_downloader_test"

SECTION_HTML = "<html><body><p>Список тем раздела</p></body></html>"

# Тема из 3 страниц: start=0,15,30
TOPIC_PAGE_0 = """<html><body>
<p>Страница 1, сообщение №1</p>
<div class="pagination">
  <a href="./viewtopic.php?f=1&amp;t=10&amp;start=0">1</a>
  <a href="./viewtopic.php?f=1&amp;t=10&amp;start=15">2</a>
  <a href="./viewtopic.php?f=1&amp;t=10&amp;start=30">3</a>
</div>
</body></html>"""

TOPIC_PAGE_15 = """<html><body>
<p>Страница 2, сообщение №16</p>
<div class="pagination">
  <a href="./viewtopic.php?f=1&amp;t=10&amp;start=0">1</a>
  <a href="./viewtopic.php?f=1&amp;t=10&amp;start=15">2</a>
  <a href="./viewtopic.php?f=1&amp;t=10&amp;start=30">3</a>
</div>
</body></html>"""

TOPIC_PAGE_30 = """<html><body>
<p>Страница 3 (последняя), сообщение №31</p>
<div class="pagination">
  <a href="./viewtopic.php?f=1&amp;t=10&amp;start=0">1</a>
  <a href="./viewtopic.php?f=1&amp;t=10&amp;start=15">2</a>
  <a href="./viewtopic.php?f=1&amp;t=10&amp;start=30">3</a>
</div>
</body></html>"""

LOGIN_FORM = """<html><body>
<form action="./ucp.php?mode=login" method="post" id="login">
<input type="text" name="username"/><input type="password" name="password"/>
<input type="hidden" name="creation_time" value="123"/>
<input type="hidden" name="form_token" value="tok"/>
<input type="hidden" name="sid" value="sidval"/>
<input type="submit" name="login" value="Вход"/>
</form></body></html>"""


class MockHandler(http.server.BaseHTTPRequestHandler):
    session_ok = set()

    def log_message(self, *a):
        pass

    def _cookie_ok(self):
        return "session=ok" in self.headers.get("Cookie", "")

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/viewforum.php":
            self._html(200, SECTION_HTML)
        elif parsed.path == "/viewtopic.php" and qs.get("t") == ["10"]:
            start = qs.get("start", ["0"])[0]
            page = {"0": TOPIC_PAGE_0, "15": TOPIC_PAGE_15, "30": TOPIC_PAGE_30}.get(start)
            self._html(200, page) if page else self._html(404, "not found")
        elif parsed.path == "/viewtopic.php" and qs.get("t") == ["99"]:
            self._html(404, "<html>Тема удалена</html>")
        elif parsed.path == "/viewtopic.php" and qs.get("t") == ["77"]:
            self._html(502, "<html>Bad Gateway</html>")
        elif parsed.path == "/viewtopic.php" and qs.get("t") == ["55"]:
            # Требует логина: без cookie отдает форму логина при 200 OK
            if self._cookie_ok():
                self._html(200, "<html><body>Секретное содержимое темы 55</body></html>")
            else:
                self._html(200, LOGIN_FORM)
        elif parsed.path == "/ucp.php" and qs.get("mode") == ["login"]:
            self._html(200, LOGIN_FORM)
        else:
            self._html(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/ucp.php" and qs.get("mode") == ["login"]:
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode())
            if fields.get("username", [""])[0] == "nate" and fields.get("password", [""])[0] == "secret":
                self.send_response(200)
                self.send_header("Set-Cookie", "session=ok")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write('<html><body><a href="?mode=logout">Выход</a></body></html>'.encode("utf-8"))
            else:
                self._html(200, LOGIN_FORM)

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


def setup_env(base_url):
    cfg = AppConfig()
    cfg.paths.archive_root = TEST_ROOT
    cfg.forum.base_url = base_url
    cfg.throttle.min_delay_seconds = 0.001
    cfg.throttle.max_delay_seconds = 0.002
    cfg.backoff.backoff_schedule_seconds = [0, 0, 0, 0]
    cfg.retry.max_attempts_per_task = 1
    ensure_directories(cfg)
    conn = connect_db(cfg.paths.db_path())
    init_db(conn)
    qm = QueueManager(conn, cfg.backoff.backoff_schedule_seconds,
                       cfg.backoff.reset_streak_after_successes,
                       cfg.retry.max_attempts_per_task)
    client = ForumClient(base_url, cfg.forum.user_agent,
                          cfg.throttle.min_delay_seconds, cfg.throttle.max_delay_seconds)
    return cfg, conn, qm, client


def main():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    server, port = start_server()
    base_url = f"http://127.0.0.1:{port}/"

    # --- Подготовка структуры (как это сделал бы Scanner) ---
    cfg, conn, qm, client = setup_env(base_url)
    conn.execute(
        "INSERT INTO sections (id, forum_id, name, url, parent_id, page_count, scanned) "
        "VALUES ('1', '1', 'TestSection', ?, NULL, 1, 1)",
        (base_url + "viewforum.php?f=1",),
    )
    conn.execute(
        "INSERT INTO topics (id, section_id, name, url, author, page_count, scanned) "
        "VALUES ('10', '1', 'МногостраничнаяТема', ?, 'author1', 1, 1)",
        (base_url + "viewtopic.php?f=1&t=10&start=0",),
    )
    conn.commit()

    qm.add_task(TYPE_SECTION_PAGE, base_url + "viewforum.php?f=1",
                "forum/sections/TestSection/page_001.html")
    qm.add_task(TYPE_TOPIC_PAGE, base_url + "viewtopic.php?f=1&t=10&start=0",
                "forum/sections/TestSection/topics/МногостраничнаяТема/page_001.html", parent_id="10")
    qm.add_task(TYPE_TOPIC_PAGE, base_url + "viewtopic.php?f=1&t=99",
                "forum/sections/TestSection/topics/Удаленная/page_001.html", parent_id="99")
    qm.add_task(TYPE_TOPIC_PAGE, base_url + "viewtopic.php?f=1&t=77",
                "forum/sections/TestSection/topics/Сломанная/page_001.html", parent_id="77")

    downloader = Downloader(cfg, conn, qm, client)
    stats = downloader.run(wait_for_backoff=False)

    # 1. Раздел + все 3 страницы темы должны быть скачаны и сохранены
    import os
    section_file = os.path.join(TEST_ROOT, "forum/sections/TestSection/page_001.html")
    assert os.path.exists(section_file), "Файл страницы раздела не сохранен"
    with open(section_file, encoding="utf-8") as f:
        assert "Список тем раздела" in f.read()
    print("[OK] Страница раздела скачана и сохранена на диск")

    page1 = os.path.join(TEST_ROOT, "forum/sections/TestSection/topics/МногостраничнаяТема/page_001.html")
    page2 = os.path.join(TEST_ROOT, "forum/sections/TestSection/topics/МногостраничнаяТема/page_002.html")
    page3 = os.path.join(TEST_ROOT, "forum/sections/TestSection/topics/МногостраничнаяТема/page_003.html")
    assert os.path.exists(page1), "Страница 1 темы не сохранена"
    assert os.path.exists(page2), "Страница 2 НЕ обнаружена автоматически!"
    assert os.path.exists(page3), "Страница 3 НЕ обнаружена автоматически!"
    with open(page2, encoding="utf-8") as f:
        assert "сообщение №16" in f.read()
    with open(page3, encoding="utf-8") as f:
        assert "последняя" in f.read()
    print("[OK] Все 3 страницы многостраничной темы обнаружены и скачаны инкрементально")

    # Проверяем, что копия-оригинал тоже сохранена (metadata/raw_html)
    raw_page2 = os.path.join(TEST_ROOT, "metadata/raw_html/forum/sections/TestSection/topics/МногостраничнаяТема/page_002.html")
    assert os.path.exists(raw_page2), "Оригинал (raw_html) страницы 2 не сохранен"
    print("[OK] Оригинальная копия HTML сохранена отдельно (metadata/raw_html)")

    # 2. Проверяем статусы задач в БД
    row99 = conn.execute("SELECT status FROM tasks WHERE parent_id = '99'").fetchone()
    assert row99["status"] == STATUS_SKIPPED_MISSING, f"Ожидался skipped_missing, получено {row99['status']}"
    print("[OK] 404 у темы 99 -> статус skipped_missing")

    row77 = conn.execute("SELECT status FROM tasks WHERE parent_id = '77'").fetchone()
    assert row77["status"] == "failed", f"Ожидался failed (max_attempts=1 в тесте), получено {row77['status']}"
    print("[OK] 502 у темы 77 -> задача провалена окончательно (backoff/retry сработали корректно)")

    # 3. Счетчик page_count у темы в таблице topics обновился
    topic_row = conn.execute("SELECT page_count FROM topics WHERE id = '10'").fetchone()
    assert topic_row["page_count"] == 3, f"Ожидался page_count=3, получено {topic_row['page_count']}"
    print("[OK] Счетчик page_count темы в БД обновлен до 3")

    print(f"\nСтатистика: {stats}")

    conn.close()

    # --- Отдельный сценарий: авто-перелогин при истекшей сессии ---
    cfg2, conn2, qm2, client2 = setup_env(base_url)
    conn2.execute(
        "INSERT INTO sections (id, forum_id, name, url, parent_id, page_count, scanned) "
        "VALUES ('2', '2', 'ClosedSection', ?, NULL, 1, 1)",
        (base_url + "viewforum.php?f=2",),
    )
    conn2.execute(
        "INSERT INTO topics (id, section_id, name, url, author, page_count, scanned) "
        "VALUES ('55', '2', 'СекретнаяТема', ?, 'admin', 1, 1)",
        (base_url + "viewtopic.php?f=1&t=55",),
    )
    conn2.commit()
    qm2.add_task(TYPE_TOPIC_PAGE, base_url + "viewtopic.php?f=1&t=55",
                 "forum/sections/ClosedSection/topics/СекретнаяТема/page_001.html", parent_id="55")

    creds = ForumCredentials(username="nate", password="secret")
    downloader2 = Downloader(cfg2, conn2, qm2, client2, credentials=creds)
    stats2 = downloader2.run(wait_for_backoff=False)

    secret_file = os.path.join(f"{TEST_ROOT}", "forum/sections/ClosedSection/topics/СекретнаяТема/page_001.html")
    # archive_root совпадает с первым сценарием (TEST_ROOT), т.к. setup_env использует ту же папку -
    # это нормально, файлы просто добавляются в то же дерево.
    assert os.path.exists(secret_file), "Страница закрытой темы не сохранена после авто-перелогина"
    with open(secret_file, encoding="utf-8") as f:
        assert "Секретное содержимое" in f.read()
    print("[OK] При истекшей сессии Downloader сам перелогинился и докачал страницу")
    print(f"Auth retries: {stats2.auth_retries}")

    conn2.close()
    server.shutdown()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)

    print("\nВСЕ ПРОВЕРКИ downloader.py ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
