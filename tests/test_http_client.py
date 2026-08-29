"""
test_http_client.py — проверка ForumClient (логин, классификация ошибок,
обнаружение "вылета" из сессии) на локальном mock-сервере, имитирующем
основные ответы phpBB. Реальный форум не используется.
"""


import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше tests/) — нужен, чтобы "from utils...."
# и подобные внутрипроектные импорты работали независимо от того, откуда
# запущен этот файл (py tests/test_x.py из корня, или py test_x.py изнутри tests/).

import http.server
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

from network.http_client import ForumClient, STATUS_SUCCESS, STATUS_MISSING, STATUS_SERVER_ERROR, STATUS_AUTH_REQUIRED

VALID_USER = "test_user"
VALID_PASS = "test_pass"

LOGIN_FORM_HTML = """
<html><body>
<form id="login" method="post" action="./ucp.php?mode=login">
  <input type="hidden" name="sid" value="abc123sid" />
  <input type="hidden" name="creation_time" value="1234567890" />
  <input type="hidden" name="form_token" value="deadbeeftoken" />
  <input type="text" name="username" />
  <input type="password" name="password" />
  <input type="submit" name="login" value="Login" />
</form>
</body></html>
"""

LOGGED_IN_INDEX_HTML = """
<html><body>
<div class="navbar">
  <a href="./ucp.php?mode=logout&amp;sid=abc123sid">Выход [ test_user ]</a>
</div>
<p>Добро пожаловать на закрытый форум</p>
</body></html>
"""


class MockPhpBBHandler(http.server.BaseHTTPRequestHandler):
    logged_in_sessions = set()

    def log_message(self, format, *args):
        pass  # тишина в консоли теста

    def _cookie_session(self):
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"session=(\w+)", cookie)
        return m.group(1) if m else None

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/ucp.php" and qs.get("mode") == ["login"]:
            self._send_html(200, LOGIN_FORM_HTML)
            return

        if parsed.path == "/closed_forum.php":
            session = self._cookie_session()
            if session in self.logged_in_sessions:
                self._send_html(200, LOGGED_IN_INDEX_HTML)
            else:
                self._send_html(200, LOGIN_FORM_HTML)  # "вылет" в форму логина
            return

        if parsed.path == "/gone.php":
            self._send_html(404, "<html>Not Found</html>")
            return

        if parsed.path == "/broken.php":
            self._send_html(502, "<html>Bad Gateway</html>")
            return

        if parsed.path == "/index.php":
            self._send_html(200, "<html><body>Публичный форум, все ок</body></html>")
            return

        self._send_html(404, "<html>Not Found</html>")

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/ucp.php" and qs.get("mode") == ["login"]:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            fields = parse_qs(body)
            username = fields.get("username", [""])[0]
            password = fields.get("password", [""])[0]
            form_token = fields.get("form_token", [""])[0]

            assert form_token == "deadbeeftoken", "Anti-CSRF поле не было передано обратно!"

            if username == VALID_USER and password == VALID_PASS:
                session_id = "sess_ok_1"
                self.logged_in_sessions.add(session_id)
                self.send_response(200)
                self.send_header("Set-Cookie", f"session={session_id}")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(LOGGED_IN_INDEX_HTML.encode("utf-8"))
            else:
                self._send_html(200, LOGIN_FORM_HTML)
            return
        self._send_html(404, "Not found")

    def _send_html(self, code, html):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


def start_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), MockPhpBBHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def main():
    server, port = start_server()
    base_url = f"http://127.0.0.1:{port}/"
    client = ForumClient(base_url, user_agent="TestAgent/1.0", min_delay=0.01, max_delay=0.02)

    # 1. Публичная страница — успех без логина
    r = client.fetch(base_url + "index.php")
    assert r.status == STATUS_SUCCESS, r.error_message
    print("[OK] Публичная страница доступна без логина")

    # 2. Закрытый раздел без логина -> должен определиться как auth_required
    #    ЛОВУШКА: сервер отвечает 200, но с формой логина внутри —
    #    именно это и должен поймать _looks_like_login_page().
    r = client.fetch(base_url + "closed_forum.php")
    assert r.status == STATUS_AUTH_REQUIRED, f"Ожидался auth_required, получено {r.status}"
    print("[OK] Закрытый раздел без логина корректно определен как auth_required (200 + login-форма)")

    # 3. Неверный логин
    ok = client.login("wrong_user", "wrong_pass")
    assert ok is False
    assert client.is_logged_in() is False
    print("[OK] Неверные учетные данные -> login() вернул False")

    # 4. Верный логин, с проверкой передачи anti-CSRF form_token
    ok = client.login(VALID_USER, VALID_PASS)
    assert ok is True
    assert client.is_logged_in() is True
    print("[OK] Верные учетные данные -> вход выполнен, form_token передан корректно")

    # 5. Теперь закрытый раздел должен быть доступен (cookie сохранилась в сессии)
    r = client.fetch(base_url + "closed_forum.php")
    assert r.status == STATUS_SUCCESS, f"Ожидался success после логина, получено {r.status}"
    assert "Добро пожаловать" in r.text
    print("[OK] После логина закрытый раздел успешно загружен той же сессией")

    # 6. 404 -> missing
    r = client.fetch(base_url + "gone.php")
    assert r.status == STATUS_MISSING
    print("[OK] 404 -> статус missing")

    # 7. 502 -> server_error
    r = client.fetch(base_url + "broken.php")
    assert r.status == STATUS_SERVER_ERROR
    print("[OK] 502 -> статус server_error")

    # 8. throttle_delay реально ждет (с маленькими значениями для скорости теста)
    start = time.time()
    client.throttle_delay()
    elapsed = time.time() - start
    assert elapsed >= 0.01
    print(f"[OK] throttle_delay отработал ({elapsed*1000:.0f} мс)")

    server.shutdown()
    print("\nВСЕ ПРОВЕРКИ http_client.py ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
