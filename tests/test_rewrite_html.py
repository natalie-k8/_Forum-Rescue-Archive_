"""
test_rewrite_html.py — проверка rewrite_html.py: подстановка локальных
путей вместо оригинальных URL после того, как ресурсы скачаны, и
идемпотентность (повторный запуск не портит результат, т.к. всегда
читает из raw_html-оригинала).
"""


import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше tests/) — нужен, чтобы "from utils...."
# и подобные внутрипроектные импорты работали независимо от того, откуда
# запущен этот файл (py tests/test_x.py из корня, или py test_x.py изнутри tests/).

import os
import shutil

from utils.config import AppConfig, ensure_directories
from storage.database import (
    connect_db, init_db, QueueManager, TYPE_TOPIC_PAGE, TYPE_IMAGE, TYPE_CSS,
)
from scanner.downloader import save_html_atomic, save_bytes_atomic
from scanner.rewrite_html import rewrite_all_pages

TEST_ROOT = "Archive_rewrite_test"
BASE_URL = "http://forum.test/"

RAW_HTML = """<html><head>
<link rel="stylesheet" href="./styles/theme.css">
</head><body>
<p>Текст сообщения</p>
<img src="./files/photo.jpg" alt="фото">
<img src="./files/not_downloaded_yet.jpg" alt="еще не скачано">
</body></html>"""


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

    page_local_path = "forum/sections/S/topics/T/page_001.html"
    raw_path = os.path.join(TEST_ROOT, "metadata", "raw_html", page_local_path)
    final_path = os.path.join(TEST_ROOT, page_local_path)
    save_html_atomic(raw_path, RAW_HTML)
    save_html_atomic(final_path, RAW_HTML)  # имитация состояния "до переписывания"

    page_task_id = qm.add_task(TYPE_TOPIC_PAGE, BASE_URL + "viewtopic.php?t=1", page_local_path)
    qm.mark_task_done(page_task_id)

    photo_task_id = qm.add_task(TYPE_IMAGE, BASE_URL + "files/photo.jpg", "images/abc_photo.jpg")
    qm.mark_task_done(photo_task_id)
    save_bytes_atomic(os.path.join(TEST_ROOT, "images/abc_photo.jpg"), b"FAKE_IMAGE_BYTES")

    css_task_id = qm.add_task(TYPE_CSS, BASE_URL + "styles/theme.css", "css/def_theme.css")
    qm.mark_task_done(css_task_id)
    save_bytes_atomic(os.path.join(TEST_ROOT, "css/def_theme.css"), b"body{color:red}")

    # not_downloaded_yet.jpg НЕ помечен done — должен остаться нетронутым

    stats = rewrite_all_pages(cfg, conn)

    assert stats.pages_rewritten == 1
    assert stats.links_replaced == 2, f"Ожидалось 2 замены (картинка+CSS), получено {stats.links_replaced}"
    print(f"[OK] Переписана 1 страница, {stats.links_replaced} ссылки заменены")

    with open(final_path, encoding="utf-8") as f:
        result_html = f.read()

    assert "../../../../../images/abc_photo.jpg" in result_html or "images/abc_photo.jpg" in result_html
    assert "http://forum.test/files/photo.jpg" not in result_html, "Оригинальный URL картинки должен быть заменен"
    assert "http://forum.test/styles/theme.css" not in result_html, "Оригинальный URL CSS должен быть заменен"
    assert "not_downloaded_yet.jpg" in result_html, \
        "Не скачанный ресурс не должен быть тронут (ссылка на сервер должна остаться)"
    print("[OK] Скачанные ресурсы заменены на локальные пути, нескачанный ресурс оставлен как есть")

    # Идемпотентность: повторный запуск с тем же состоянием дает тот же результат
    stats2 = rewrite_all_pages(cfg, conn)
    with open(final_path, encoding="utf-8") as f:
        result_html2 = f.read()
    assert result_html == result_html2, "Повторный запуск должен давать идентичный результат (не портить файл)"
    print("[OK] Повторный запуск идемпотентен — результат не меняется")

    conn.close()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)

    print("\nВСЕ ПРОВЕРКИ rewrite_html.py ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
