"""
test_long_paths.py — проверка сохранения файлов по путям длиннее лимита
Windows MAX_PATH (~260 символов).

Найдено при реальном прогоне форума №1: FileNotFoundError при попытке
сохранить страницу темы с глубокой вложенностью
(forum/sections/<раздел>/<подраздел>/topics/<тема>/page_001.html.tmp) —
каждое отдельное имя папки короче лимита (safe_folder_name обрезает до
80 символов), но суммарная длина всего пути (с учётом абсолютного пути
диска и папки архива) превышает 260 символов. Фикс: save_html_atomic/
save_bytes_atomic в scanner/downloader.py применяют префикс \\\\?\\ к
абсолютному пути на Windows, снимающий это ограничение у Win32 API.
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import shutil

from scanner.downloader import save_html_atomic, save_bytes_atomic

TEST_ROOT = "Archive_long_paths_test"


def main():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)

    # Строим путь длиннее 260 символов: несколько вложенных папок с
    # длинными именами (в реальности — раздел/подраздел/тема из
    # safe_folder_name, max_len=80 на каждую).
    deep_names = ["A" * 80, "B" * 80, "C" * 80, "D" * 80]
    long_path = _os.path.join(TEST_ROOT, "forum", "sections", *deep_names, "page_001.html")

    assert len(_os.path.abspath(long_path)) > 260, (
        f"Тестовый путь должен превышать 260 символов, получено "
        f"{len(_os.path.abspath(long_path))}"
    )

    save_html_atomic(long_path, "<html>тестовое содержимое</html>")
    assert _os.path.exists(long_path), "Файл по длинному пути не был сохранён"
    with open(long_path, encoding="utf-8") as f:
        assert "тестовое содержимое" in f.read()
    print(f"[OK] HTML-файл сохранён по пути длиной {len(_os.path.abspath(long_path))} символов (> 260)")

    long_bin_path = _os.path.join(TEST_ROOT, "images", *deep_names, "photo.jpg")
    save_bytes_atomic(long_bin_path, b"\xff\xd8\xff\xe0fake-jpeg-bytes")
    assert _os.path.exists(long_bin_path), "Бинарный файл по длинному пути не был сохранён"
    print(f"[OK] Бинарный файл сохранён по пути длиной {len(_os.path.abspath(long_bin_path))} символов (> 260)")

    # Обычный, короткий путь тоже должен продолжать работать как раньше.
    short_path = _os.path.join(TEST_ROOT, "forum", "sections", "Раздел", "page_001.html")
    save_html_atomic(short_path, "<html>короткий путь</html>")
    assert _os.path.exists(short_path), "Файл по обычному короткому пути не был сохранён"
    print("[OK] Обычный короткий путь по-прежнему сохраняется без изменений в поведении")

    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    print("\nВСЕ ПРОВЕРКИ длинных путей (Windows MAX_PATH) ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
