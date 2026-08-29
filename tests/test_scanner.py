"""
test_scanner.py — проверка Scanner на мок-сервере, эмулирующем:
    - открытый раздел (f=1) с подфорумом (f=3) и пагинацией списка тем;
    - закрытый раздел (f=2), доступный только после логина;
    - дедупликацию/устойчивость к повторному запуску (resumability).
"""


import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше tests/) — нужен, чтобы "from utils...."
# и подобные внутрипроектные импорты работали независимо от того, откуда
# запущен этот файл (py tests/test_x.py из корня, или py test_x.py изнутри tests/).

import http.server
import re
import shutil
import threading
from urllib.parse import parse_qs, urlparse

from utils.config import AppConfig, ensure_directories
from storage.database import connect_db, init_db, QueueManager, TYPE_SECTION_PAGE, TYPE_TOPIC_PAGE
from network.http_client import ForumClient
from utils.logger_setup import setup_logging
from scanner.scanner import Scanner

TEST_ROOT = "Archive_scanner_test"

INDEX_HTML = """
<html><body>
<a href="./viewforum.php?f=1" class="forumlink">Открытый раздел</a>
<a href="./viewforum.php?f=2" class="forumlink">Закрытый раздел</a>
</body></html>
"""

VIEWFORUM_1_PAGE0 = """
<html><body>
<a href="./viewforum.php?f=3">Подфорум: Живопись</a>
<table>
<tr><td><a href="./viewtopic.php?f=1&amp;t=10" class="topictitle">Тема А</a></td>
    <td><a href="./memberlist.php?mode=viewprofile&amp;u=1">user1</a></td></tr>
<tr><td><a href="./viewtopic.php?f=1&amp;t=11" class="topictitle">Тема Б</a></td>
    <td><a href="./memberlist.php?mode=viewprofile&amp;u=2">user2</a></td></tr>
</table>
<div class="pagination">
  <a href="./viewforum.php?f=1&amp;start=0">1</a>,
  <a href="./viewforum.php?f=1&amp;start=2">2</a>
</div>
</body></html>
"""

VIEWFORUM_1_PAGE2 = """
<html><body>
<table>
<tr><td><a href="./viewtopic.php?f=1&amp;t=12" class="topictitle">Тема В</a></td>
    <td><a href="./memberlist.php?mode=viewprofile&amp;u=3">user3</a></td></tr>
</table>
<div class="pagination">
  <a href="./viewforum.php?f=1&amp;start=0">1</a>,
  <a href="./viewforum.php?f=1&amp;start=2">2</a>
</div>
</body></html>
"""

LOGIN_FORM_HTML = """
<html><body>
<form id="login" method="post" action="./ucp.php?mode=login">
  <input type="hidden" name="form_token" value="tok" />
  <input type="text" name="username" />
  <input type="password" name="password" />
  <input type="submit" name="login" value="Login" />
</form>
</body></html>
"""

VIEWFORUM_2_HTML = """
<html><body>
<table>
<tr><td><a href="./viewtopic.php?f=2&amp;t=20" class="topictitle">Секретная тема</a></td>
    <td><a href="./memberlist.php?mode=viewprofile&amp;u=99">admin</a></td></tr>
</table>
</body></html>
"""


class MockHandler(http.server.BaseHTTPRequestHandler):
    request_log = []

    def log_message(self, format, *args):
        pass

    def _session_ok(self):
        cookie = self.headers.get("Cookie", "")
        return "session=okforum2" in cookie

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        MockHandler.request_log.append(self.path)

        if parsed.path == "/index.php":
            self._html(200, INDEX_HTML)
        elif parsed.path == "/viewforum.php" and qs.get("f") == ["1"]:
            start = qs.get("start", ["0"])[0]
            self._html(200, VIEWFORUM_1_PAGE0 if start == "0" else VIEWFORUM_1_PAGE2)
        elif parsed.path == "/viewforum.php" and qs.get("f") == ["3"]:
            self._html(200, "<html><body>Пустой подфорум</body></html>")
        elif parsed.path == "/viewforum.php" and qs.get("f") == ["2"]:
            if self._session_ok():
                self._html(200, VIEWFORUM_2_HTML)
            else:
                self._html(200, LOGIN_FORM_HTML)
        elif parsed.path == "/ucp.php" and qs.get("mode") == ["login"]:
            self._html(200, LOGIN_FORM_HTML)
        else:
            self._html(404, "<html>Not found</html>")

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/ucp.php" and qs.get("mode") == ["login"]:
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            if fields.get("username", [""])[0] == "nate" and fields.get("password", [""])[0] == "secret":
                self.send_response(200)
                self.send_header("Set-Cookie", "session=okforum2")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write('<html><body><a href="?mode=logout">Выход [nate]</a></body></html>'.encode("utf-8"))
            else:
                self._html(200, LOGIN_FORM_HTML)

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


def make_scanner(base_url, db_suffix):
    cfg = AppConfig()
    cfg.paths.archive_root = f"{TEST_ROOT}_{db_suffix}"
    cfg.forum.base_url = base_url
    ensure_directories(cfg)
    conn = connect_db(cfg.paths.db_path())
    init_db(conn)
    qm = QueueManager(conn, backoff_schedule=cfg.backoff.backoff_schedule_seconds,
                       reset_streak_after_successes=cfg.backoff.reset_streak_after_successes,
                       max_attempts_per_task=cfg.retry.max_attempts_per_task)
    client = ForumClient(base_url, user_agent="TestAgent/1.0", min_delay=0.001, max_delay=0.002)
    scanner = Scanner(cfg, conn, qm, client)
    return cfg, conn, qm, client, scanner


def main():
    shutil.rmtree(f"{TEST_ROOT}_a", ignore_errors=True)
    shutil.rmtree(f"{TEST_ROOT}_b", ignore_errors=True)
    setup_logging("logs_scanner_test")

    server, port = start_server()
    base_url = f"http://127.0.0.1:{port}/"

    # --- Сценарий A: без логина, полное сканирование доступных разделов ---
    cfg, conn, qm, client, scanner = make_scanner(base_url, "a")
    stats = scanner.run()

    sections = conn.execute("SELECT id, name, scanned, parent_id FROM sections").fetchall()
    sections_by_id = {r["id"]: r for r in sections}
    assert set(sections_by_id.keys()) == {"1", "2", "3"}, sections_by_id.keys()
    assert sections_by_id["3"]["parent_id"] == "1", "Подфорум должен ссылаться на родителя f=1"
    assert all(r["scanned"] == 1 for r in sections), "Все известные разделы должны быть помечены как отсканированные"
    print(f"[OK] Найдено {len(sections)} раздела (включая подфорум), все помечены scanned")

    topics = conn.execute("SELECT id, name, author, section_id FROM topics").fetchall()
    topic_ids = {r["id"] for r in topics}
    assert topic_ids == {"10", "11", "12"}, topic_ids  # тема 20 из закрытого раздела недоступна без логина
    print(f"[OK] Найдено {len(topics)} темы из открытого раздела (закрытый раздел пропущен без логина)")

    authors = {r["id"]: r["author"] for r in topics}
    assert authors["10"] == "user1" and authors["12"] == "user3"
    print("[OK] Авторы тем определены верно")

    counts = qm.get_counts()
    section_page_tasks = counts["by_type_status"].get(TYPE_SECTION_PAGE, {})
    topic_page_tasks = counts["by_type_status"].get(TYPE_TOPIC_PAGE, {})
    assert sum(section_page_tasks.values()) == 3, "Ожидалось 3 страницы списка (2 из f=1 + 1 из подфорума f=3)"
    assert sum(topic_page_tasks.values()) == 3, "Ожидалось 3 задачи на страницы тем (10, 11, 12)"
    print(f"[OK] В очередь поставлено {sum(section_page_tasks.values())} section_page и {sum(topic_page_tasks.values())} topic_page задач")

    # --- Проверка resumability: повторный запуск переспрашивает только
    # главную страницу (вдруг появились новые разделы верхнего уровня),
    # но не трогает уже известные разделы и не сбрасывает их scanned ---
    requests_before = len(MockHandler.request_log)
    stats2 = scanner.run()
    requests_after = len(MockHandler.request_log)
    assert requests_after == requests_before + 1, (
        "Повторный запуск должен сделать РОВНО один запрос — к главной "
        "странице, чтобы проверить новые разделы верхнего уровня; уже "
        "известные разделы повторно не запрашиваются"
    )
    assert stats2.stopped_reason == "Все известные разделы отсканированы"
    sections_after_rerun = conn.execute("SELECT id, scanned FROM sections").fetchall()
    assert all(r["scanned"] == 1 for r in sections_after_rerun), (
        "Повторный запуск не должен сбрасывать scanned у уже известных разделов"
    )
    print("[OK] Повторный запуск проверяет главную страницу на новые разделы, но не пересканирует уже известные")

    conn.close()

    # --- Сценарий B: с логином, доступен и закрытый раздел ---
    cfg_b, conn_b, qm_b, client_b, scanner_b = make_scanner(base_url, "b")
    logged_in = client_b.login("nate", "secret")
    assert logged_in is True
    scanner_b.run()

    topics_b = conn_b.execute("SELECT id FROM topics").fetchall()
    topic_ids_b = {r["id"] for r in topics_b}
    assert "20" in topic_ids_b, "После логина тема из закрытого раздела должна быть найдена"
    print("[OK] С валидным логином закрытый раздел отсканирован, секретная тема найдена")

    conn_b.close()
    server.shutdown()

    shutil.rmtree(f"{TEST_ROOT}_a", ignore_errors=True)
    shutil.rmtree(f"{TEST_ROOT}_b", ignore_errors=True)
    shutil.rmtree("logs_scanner_test", ignore_errors=True)

    print("\nВСЕ ПРОВЕРКИ scanner.py ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
