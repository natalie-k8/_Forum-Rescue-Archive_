"""
test_css_imports.py — проверка обнаружения и переписывания @import/url(...)
ВНУТРИ уже скачанных CSS-файлов (см. scanner/css_links.py).

Контекст бага: типичный phpBB style.css сам почти пустой и подключает
другие файлы (normalize.css, colours.css, ...) через @import url("...").
Сканер видел только <link rel="stylesheet"> в HTML, поэтому такие
"вложенные" CSS-файлы никогда не попадали в очередь на скачивание —
архив оставался без оформления, даже когда сам style.css был успешно
скачан и HTML был правильно переписан на локальный путь до него.

Две части:
    1. Downloader обнаруживает @import внутри только что скачанного CSS
       и ставит найденные файлы в очередь.
    2. rewrite_all_pages переписывает @import/url(...) внутри уже
       скачанного CSS-файла на локальные пути, когда вложенные файлы
       тоже скачаны — и оставляет нетронутым то, что ещё не скачано.
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import http.server
import os
import shutil
import threading
from urllib.parse import urlparse

from utils.config import AppConfig, ensure_directories
from storage.database import connect_db, init_db, QueueManager, TYPE_TOPIC_PAGE, TYPE_CSS
from scanner.downloader import Downloader
from scanner.rewrite_html import rewrite_all_pages
from network.http_client import ForumClient

TEST_ROOT = "Archive_css_import_test"

TOPIC_HTML = """<html><head>
<link rel="stylesheet" href="./styles/style.css">
</head><body><p>Текст сообщения</p></body></html>"""

# Типичный "почти пустой" phpBB style.css — вся реальная работа через @import.
STYLE_CSS = """
@import url("normalize.css?v=3.2");
@import url("colours.css?v=3.2");
"""

NORMALIZE_CSS = "html{margin:0}"
# colours.css дополнительно ссылается на фоновую картинку через url(...) —
# проверяет, что url() без @import тоже обнаруживается и переписывается.
COLOURS_CSS = 'body{background:url("bg.png") repeat}'


class MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        routes = {
            "/viewtopic.php": ("text/html; charset=utf-8", TOPIC_HTML.encode("utf-8")),
            "/styles/style.css": ("text/css", STYLE_CSS.encode("utf-8")),
            "/styles/normalize.css": ("text/css", NORMALIZE_CSS.encode("utf-8")),
            "/styles/colours.css": ("text/css", COLOURS_CSS.encode("utf-8")),
        }
        if parsed.path in routes:
            content_type, body = routes[parsed.path]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def start_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def setup_env(base_url):
    cfg = AppConfig()
    cfg.paths.archive_root = TEST_ROOT
    cfg.forum.base_url = base_url
    cfg.mode.mode = 2
    cfg.throttle.min_delay_seconds = 0.001
    cfg.throttle.max_delay_seconds = 0.002
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

    cfg, conn, qm, client = setup_env(base_url)
    qm.add_task(TYPE_TOPIC_PAGE, base_url + "viewtopic.php?f=1&t=1",
                "forum/sections/S/topics/T/page_001.html")

    downloader = Downloader(cfg, conn, qm, client)
    stats = downloader.run(wait_for_backoff=False)

    # style.css обнаружен со страницы; normalize.css и colours.css должны
    # быть обнаружены ПОСЛЕ скачивания style.css, через его @import —
    # без фикса это 1 (только style.css), с фиксом 3.
    css_tasks = conn.execute("SELECT * FROM tasks WHERE type = ?", (TYPE_CSS,)).fetchall()
    # url в задаче включает "?v=3.2" из самой ссылки @import — сравниваем
    # по base_url-префиксу, а не по точному совпадению.
    css_urls = {row["url"] for row in css_tasks}
    assert base_url + "styles/style.css" in css_urls
    assert any(u.startswith(base_url + "styles/normalize.css") for u in css_urls), \
        "normalize.css (@import внутри style.css) должен быть обнаружен и поставлен в очередь"
    assert any(u.startswith(base_url + "styles/colours.css") for u in css_urls), \
        "colours.css (@import внутри style.css) должен быть обнаружен и поставлен в очередь"
    print(f"[OK] Обнаружено {len(css_urls)} CSS-файлов, включая вложенные через @import")

    assert all(row["status"] == "done" for row in css_tasks), \
        "Все CSS-файлы (включая вложенные) должны быть скачаны в режиме 2"
    print("[OK] Все CSS-файлы, включая обнаруженные через @import, скачаны")

    style_task = next(row for row in css_tasks if row["url"] == base_url + "styles/style.css")
    style_path = os.path.join(TEST_ROOT, style_task["local_path"])
    with open(style_path, "rb") as f:
        assert f.read() == STYLE_CSS.encode("utf-8"), "style.css должен быть сохранен как есть до переписывания"

    # --- Переписывание: @import и url(...) внутри style.css/colours.css
    # должны замениться на локальные относительные пути ---
    rewrite_stats = rewrite_all_pages(cfg, conn)
    assert rewrite_stats.css_files_rewritten >= 1, "Хотя бы один CSS-файл должен быть переписан"
    print(f"[OK] Переписано {rewrite_stats.css_files_rewritten} CSS-файлов, "
          f"{rewrite_stats.css_links_replaced} ссылок внутри CSS заменено")

    with open(style_path, "rb") as f:
        rewritten_style = f.read().decode("utf-8")
    assert "http://" not in rewritten_style, \
        f"В style.css не должно остаться абсолютных URL после переписывания: {rewritten_style!r}"
    assert "normalize.css" in rewritten_style and "colours.css" in rewritten_style, \
        "Имена файлов должны остаться узнаваемыми в переписанных относительных путях"
    print("[OK] @import в style.css переписан на локальные относительные пути")

    colours_task = next(row for row in css_tasks if row["url"].startswith(base_url + "styles/colours.css"))
    colours_path = os.path.join(TEST_ROOT, colours_task["local_path"])
    with open(colours_path, "rb") as f:
        rewritten_colours = f.read().decode("utf-8")
    # bg.png сам никогда не скачивался в этом тесте — url(...) должен
    # остаться нетронутым (относительным "bg.png", как в оригинале),
    # а не превратиться в мусор из-за попытки резолва.
    assert "bg.png" in rewritten_colours, \
        f"Нескачанный ресурс в url(...) должен остаться как есть: {rewritten_colours!r}"
    print("[OK] url(...) без @import тоже разбирается; нескачанный ресурс внутри CSS остаётся нетронутым")

    # Идемпотентность: повторный проход не портит уже переписанный файл.
    rewrite_stats2 = rewrite_all_pages(cfg, conn)
    with open(style_path, "rb") as f:
        rewritten_style2 = f.read().decode("utf-8")
    assert rewritten_style == rewritten_style2, "Повторное переписывание CSS не должно менять уже переписанный файл"
    print("[OK] Повторное переписывание CSS идемпотентно")

    conn.close()
    server.shutdown()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)

    print("\nВСЕ ПРОВЕРКИ @import ВНУТРИ CSS ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
