"""
main.py — единая точка входа Forum Rescue Archive.

Показывает текстовое меню и запускает выбранный инструмент. Логика самих
инструментов не менялась ни на строчку — main.py только вызывает их
существующие функции main() из соответствующих модулей. Это сделано
специально: если в будущем текстовое меню заменится на окно (например,
Streamlit-интерфейс из этапа 6), менять нужно будет только этот файл —
остальной код инструментов трогать не придётся.

Запуск:
    py main.py
"""

from __future__ import annotations

from run_commands import (
    run_archive,
    run_forum2x2_archive,
    run_scan_test,
    run_download_test,
    run_forum2x2_download_test,
    run_forum2x2_smoketest,
    debug_login,
)

from utils.config import load_config, save_config

MODE_LABELS = {
    1: "Спасение текста (только текст сообщений и структура форума)",
    2: "Полный архив (плюс картинки, вложения, аватары, смайлики, CSS-оформление)",
}

MENU = [
    ("Боевая архивация форума №1 (run_archive)", run_archive),
    ("Боевая архивация форума №2 forum2x2 (run_forum2x2_archive)", run_forum2x2_archive),
    ("Тестовый скан форума №1, без скачивания (run_scan_test)", run_scan_test),
    ("Тестовое скачивание форума №1 (run_download_test)", run_download_test),
    ("Тестовое скачивание forum2x2 (run_forum2x2_download_test)", run_forum2x2_download_test),
    ("Пробный запрос одной страницы forum2x2, без сохранения (run_forum2x2_smoketest)", run_forum2x2_smoketest),
    ("Диагностика входа на форум №1 (debug_login)", debug_login),
]

CHANGE_MODE_CHOICE = "M"


def change_mode() -> None:
    """
    Меняет режим (1/2) в config.json форума №1 — тот же config.json,
    который используют run_archive.py и тестовые прогоны форума №1.
    Режим для forum2x2 задаётся отдельно, при каждом запуске
    run_forum2x2_archive.py (свой config на каждый форум на хостинге).
    """
    cfg = load_config()
    print(f"\nТекущий режим: {cfg.mode.mode} — {MODE_LABELS[cfg.mode.mode]}\n")
    for i, label in MODE_LABELS.items():
        print(f"  {i}. {label}")
    choice = input("\nНовый режим (пусто — оставить как есть): ").strip()
    if not choice:
        print("Режим не изменён.")
        return
    if choice not in ("1", "2"):
        print("Нет такого режима — введи 1 или 2.")
        return
    cfg.mode.mode = int(choice)
    save_config(cfg)
    print(f"Режим сохранён: {cfg.mode.mode} — {MODE_LABELS[cfg.mode.mode]}")


def run_choice(module) -> None:
    """
    Вызывает main() у уже импортированного модуля — тот же самый код,
    что раньше запускался напрямую как 'py run_archive.py' и т.п., без
    единого изменения внутри. Модули импортируются обычным образом
    сверху файла (а не по строке во время выполнения), чтобы вся эта
    точка входа надёжно собиралась в один .exe через PyInstaller —
    ему нужно видеть импорты статически, при анализе кода.
    """
    module.main()


def main():
    print("=== Forum Rescue Archive ===\n")
    current_mode = load_config().mode.mode
    print(f"Текущий режим (форум №1): {current_mode} — {MODE_LABELS[current_mode]}\n")
    print("Что запустить?\n")
    for i, (label, _) in enumerate(MENU, start=1):
        print(f"  {i}. {label}")
    print(f"  {CHANGE_MODE_CHOICE}. Сменить режим (Спасение текста / Полный архив)")
    print("  0. Выход\n")

    choice = input("Номер пункта: ").strip()
    if choice == "0" or not choice:
        print("Пока!")
        return

    if choice.upper() == CHANGE_MODE_CHOICE:
        change_mode()
        return

    if not choice.isdigit() or not (1 <= int(choice) <= len(MENU)):
        print("Нет такого пункта — запусти main.py заново и выбери число из списка.")
        return

    _, module = MENU[int(choice) - 1]
    print()
    run_choice(module)


if __name__ == "__main__":
    main()
