"""
test_resource_downloading.py — проверка полного цикла: страница темы
содержит картинку/вложение -> Downloader обнаруживает ресурс, ставит в
очередь, скачивает бинарные данные и сохраняет на диск с правильным
содержимым. Проверяется и режим 1 (ресурсы НЕ скачиваются, только
обнаруживаются) и режим 2 (скачиваются).
"""


import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше tests/) — нужен, чтобы "from utils...."
# и подобные внутрипроектные импорты работали независимо от того, откуда
# запущен этот файл (py tests/test_x.py из корня, или py test_x.py изнутри tests/).

import http.server
import os
import shutil
import threading
from urllib.parse import urlparse

from utils.config import AppConfig, ensure_directories
from storage.database import connect_db, init_db, QueueManager, TYPE_TOPIC_PAGE, TYPE_IMAGE
from scanner.downloader import Downloader
from network.http_client import ForumClient

TEST_ROOT = "Archive_resource_test"

FAKE_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"FAKEJPEGDATA" * 20 + b"\xff\xd9"

TOPIC_HTML = """<html><body>
<p>Сообщение с картинкой:</p>
<img src="./files/museum_photo.jpg" alt="фото">
</body></html>"""


class MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/viewtopic.php":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(TOPIC_HTML.encode("utf-8"))
        elif parsed.path == "/files/museum_photo.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(FAKE_JPEG_BYTES)
        else:
            self.send_response(404)
            self.end_headers()


def start_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def setup_env(base_url, mode):
    cfg = AppConfig()
    cfg.paths.archive_root = TEST_ROOT
    cfg.forum.base_url = base_url
    cfg.mode.mode = mode
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

    # --- Режим 1: текст скачивается, ресурс только ОБНАРУЖИВАЕТСЯ, не скачивается ---
    cfg, conn, qm, client = setup_env(base_url, mode=1)
    qm.add_task(TYPE_TOPIC_PAGE, base_url + "viewtopic.php?f=1&t=1",
                "forum/sections/S/topics/T/page_001.html")

    downloader = Downloader(cfg, conn, qm, client)
    stats = downloader.run(wait_for_backoff=False)

    assert stats.pages_saved == 1
    assert stats.resources_discovered == 1, "Картинка должна быть обнаружена и поставлена в очередь"
    assert stats.resources_saved == 0, "В режиме 1 ресурсы не должны скачиваться"

    image_task = conn.execute("SELECT * FROM tasks WHERE type = ?", (TYPE_IMAGE,)).fetchone()
    assert image_task is not None
    assert image_task["status"] == "pending", "Задача на картинку должна остаться pending в режиме 1"
    print("[OK] Режим 1: текст скачан, картинка обнаружена и поставлена в очередь, но НЕ скачана")

    image_path_on_disk = os.path.join(TEST_ROOT, image_task["local_path"])
    assert not os.path.exists(image_path_on_disk), "Файл картинки не должен существовать в режиме 1"
    print("[OK] Файла картинки на диске действительно нет (режим 1)")

    conn.close()

    # --- Режим 2: та же очередь (продолжаем с той же БД), но теперь ресурсы скачиваются ---
    cfg2, conn2, qm2, client2 = setup_env(base_url, mode=2)
    downloader2 = Downloader(cfg2, conn2, qm2, client2)
    stats2 = downloader2.run(wait_for_backoff=False)

    assert stats2.resources_saved == 1, f"Ожидалось 1 скачанное изображение, получено {stats2.resources_saved}"
    print("[OK] Режим 2: ранее обнаруженная картинка теперь скачана")

    image_task2 = conn2.execute("SELECT * FROM tasks WHERE type = ?", (TYPE_IMAGE,)).fetchone()
    assert image_task2["status"] == "done"
    image_path2 = os.path.join(TEST_ROOT, image_task2["local_path"])
    assert os.path.exists(image_path2), "Файл картинки должен появиться на диске в режиме 2"
    with open(image_path2, "rb") as f:
        saved_bytes = f.read()
    assert saved_bytes == FAKE_JPEG_BYTES, "Содержимое сохраненного файла не совпадает с оригиналом"
    print(f"[OK] Файл картинки сохранен на диск с точным бинарным содержимым: {image_task2['local_path']}")

    conn2.close()
    server.shutdown()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)

    print("\nВСЕ ПРОВЕРКИ СКАЧИВАНИЯ РЕСУРСОВ ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
