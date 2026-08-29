"""
test_resources.py — проверка extract_resource_links (phpbb_parser.py) и
resource_local_path (archive_paths.py): классификация картинок/вложений/
аватаров/смайликов/CSS и построение безопасных имен файлов.
"""


import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше tests/) — нужен, чтобы "from utils...."
# и подобные внутрипроектные импорты работали независимо от того, откуда
# запущен этот файл (py tests/test_x.py из корня, или py test_x.py изнутри tests/).

from parsers.phpbb_parser import extract_resource_links, RESOURCE_TYPE_AVATAR, RESOURCE_TYPE_ATTACHMENT, \
    RESOURCE_TYPE_SMILEY, RESOURCE_TYPE_IMAGE, RESOURCE_TYPE_CSS
from storage.archive_paths import resource_local_path

BASE_URL = "http://forum.test/"

PAGE_HTML = """
<html><body>
<img src="./download/file.php?avatar=57" class="avatar">
<img src="./images/smilies/icon_smile.gif" alt=":)">
<img src="./files/photo_2024.jpg" alt="фото">
<a href="./download/file.php?id=42">document.pdf (2.3 МБ)</a>
<link rel="stylesheet" href="./styles/prosilver/theme/stylesheet.css?v=70">
<img src="./styles/prosilver/theme/images/icon_topic_latest.gif">
<img src="./assets/font-awesome/some-icon.svg">
<img src="./files/photo_2024.jpg" alt="дубликат той же картинки">
</body></html>
"""


def main():
    links = extract_resource_links(PAGE_HTML, BASE_URL)
    by_type = {}
    for l in links:
        by_type.setdefault(l.type, []).append(l.url)

    assert by_type.get(RESOURCE_TYPE_AVATAR) == ["http://forum.test/download/file.php?avatar=57"]
    print("[OK] Аватар распознан верно")

    assert by_type.get(RESOURCE_TYPE_SMILEY) == ["http://forum.test/images/smilies/icon_smile.gif"]
    print("[OK] Смайлик распознан верно")

    assert by_type.get(RESOURCE_TYPE_ATTACHMENT) == ["http://forum.test/download/file.php?id=42"]
    print("[OK] Вложение распознано верно (не спутано с аватаром)")

    assert by_type.get(RESOURCE_TYPE_CSS) == ["http://forum.test/styles/prosilver/theme/stylesheet.css?v=70"]
    print("[OK] CSS распознан верно")

    images = by_type.get(RESOURCE_TYPE_IMAGE, [])
    assert images == ["http://forum.test/files/photo_2024.jpg"], images
    print("[OK] Обычная картинка сообщения распознана, дубликат не создал вторую запись, "
          "служебная графика темы (icon_*, /assets/) отфильтрована")

    total_unique = sum(len(v) for v in by_type.values())
    assert total_unique == 5, f"Ожидалось 5 уникальных ресурсов, получено {total_unique}"
    print(f"[OK] Всего уникальных ресурсов: {total_unique} (дубликат и служебная графика не считаются)")

    # Пути к файлам: осмысленные, без коллизий, без "file.php" в имени
    avatar_path = resource_local_path(RESOURCE_TYPE_AVATAR, by_type[RESOURCE_TYPE_AVATAR][0])
    assert avatar_path.startswith("avatars/") and "file.php" not in avatar_path and "57" in avatar_path
    print(f"[OK] Путь аватара осмысленный: {avatar_path}")

    attachment_path = resource_local_path(RESOURCE_TYPE_ATTACHMENT, by_type[RESOURCE_TYPE_ATTACHMENT][0])
    assert attachment_path.startswith("attachments/") and "file.php" not in attachment_path
    print(f"[OK] Путь вложения осмысленный: {attachment_path}")

    image_path = resource_local_path(RESOURCE_TYPE_IMAGE, images[0])
    assert image_path.startswith("images/") and image_path.endswith(".jpg")
    print(f"[OK] Путь картинки сохраняет расширение: {image_path}")

    # Два разных URL с одинаковым basename не должны коллизировать
    path_a = resource_local_path(RESOURCE_TYPE_IMAGE, "http://forum.test/topic1/photo.jpg")
    path_b = resource_local_path(RESOURCE_TYPE_IMAGE, "http://forum.test/topic2/photo.jpg")
    assert path_a != path_b, "Разные URL с одинаковым именем файла не должны давать одинаковый путь"
    print(f"[OK] Одинаковые имена файлов из разных URL не коллизируют: {path_a} != {path_b}")

    print("\nВСЕ ПРОВЕРКИ РЕСУРСОВ ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
