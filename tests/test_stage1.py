"""
test_stage1.py — проверка модулей Config + Logger + Database.

Симулирует:
    1. Загрузку конфигурации и создание структуры папок.
    2. Инициализацию БД.
    3. Добавление задач в очередь (страницы + изображения), включая
       проверку дедупликации (повторное добавление того же URL).
    4. Успешную обработку задачи.
    5. Три подряд ошибки сервера -> проверка нарастающего backoff
       (10 мин -> 30 мин -> 1 час) и блокировки выдачи новых задач.
    6. "Падение программы" (эмуляция) и восстановление зависших in_progress
       задач при следующем запуске.
    7. Запись progress_state.json.
"""


import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше tests/) — нужен, чтобы "from utils...."
# и подобные внутрипроектные импорты работали независимо от того, откуда
# запущен этот файл (py tests/test_x.py из корня, или py test_x.py изнутри tests/).

import shutil
from datetime import datetime, timedelta, timezone

from utils.config import load_config, save_config, ensure_directories
from utils.logger_setup import setup_logging
from storage.database import (
    connect_db, init_db, QueueManager, write_progress_snapshot,
    TYPE_SECTION_PAGE, TYPE_TOPIC_PAGE, TYPE_IMAGE, kv_set,
)

TEST_ROOT = "Archive_test"


def reset_test_env():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def main():
    reset_test_env()

    # 1. Конфигурация
    cfg = load_config("config_test.json")
    cfg.paths.archive_root = TEST_ROOT
    cfg.test_mode.enabled = True
    save_config(cfg, "config_test.json")
    ensure_directories(cfg)
    print("[OK] Конфигурация загружена и папки созданы:", cfg.paths.archive_root)

    # 2. Логирование
    logger = setup_logging(cfg.paths.logs_dir())
    logger.info("=== Тестовый запуск Stage 1 начат ===")

    # 3. База данных
    conn = connect_db(cfg.paths.db_path())
    init_db(conn)
    qm = QueueManager(
        conn,
        backoff_schedule=cfg.backoff.backoff_schedule_seconds,
        reset_streak_after_successes=cfg.backoff.reset_streak_after_successes,
        max_attempts_per_task=cfg.retry.max_attempts_per_task,
    )

    # 4. Добавление задач + проверка дедупликации
    t1 = qm.add_task(TYPE_SECTION_PAGE, "https://forum.test/viewforum.php?f=1", "forum/sections/general/page_001.html")
    t2 = qm.add_task(TYPE_TOPIC_PAGE, "https://forum.test/viewtopic.php?t=10", "forum/sections/general/topics/topic_10/page_001.html")
    t3 = qm.add_task(TYPE_IMAGE, "https://forum.test/images/photo1.jpg", "images/photo1.jpg", parent_id=t2)
    t1_dup = qm.add_task(TYPE_SECTION_PAGE, "https://forum.test/viewforum.php?f=1", "forum/sections/general/page_001.html")
    assert t1 == t1_dup, "Дедупликация не сработала!"
    counts = qm.get_counts()
    total_tasks = sum(counts["by_status"].values())
    assert total_tasks == 3, f"Ожидалось 3 задачи, получено {total_tasks}"
    print(f"[OK] Добавлено 3 уникальные задачи, дубликат не создан (id={t1[:8]}...)")

    # 5. Успешная обработка первой задачи (режим 1 -> только текстовые задачи, priority=1)
    task = qm.get_next_task(priority_ceiling=cfg.mode.priority_ceiling())
    assert task is not None and task.type == TYPE_SECTION_PAGE
    qm.mark_task_done(task.id)
    print(f"[OK] Задача {task.type} успешно обработана и помечена done")

    # 6. Симуляция трех ошибок сервера подряд (502, 503, timeout)
    task2 = qm.get_next_task(priority_ceiling=cfg.mode.priority_ceiling())
    assert task2 is not None and task2.type == TYPE_TOPIC_PAGE

    errors = ["502 Bad Gateway", "503 Service Unavailable", "Connection timeout"]
    expected_delays = [600, 1800, 3600]  # 10 мин, 30 мин, 1 час
    for i, err in enumerate(errors):
        qm.mark_task_failed(task2.id, err)
        counts = qm.get_counts()
        streak = counts["error_streak"]
        assert streak == i + 1, f"Ожидался error_streak={i+1}, получено {streak}"
        row = conn.execute("SELECT next_attempt_at FROM tasks WHERE id=?", (task2.id,)).fetchone()
        next_attempt = datetime.fromisoformat(row["next_attempt_at"])
        delay = (next_attempt - datetime.now(timezone.utc)).total_seconds()
        assert abs(delay - expected_delays[i]) < 5, f"Backoff #{i+1}: ожидалось ~{expected_delays[i]}с, получено {delay:.0f}с"
        print(f"[OK] Ошибка #{i+1} ({err}) -> backoff ~{expected_delays[i]/60:.0f} мин, error_streak={streak}")

    # 7. Проверка, что сервер сейчас "отдыхает" -> новые задачи не выдаются
    assert qm.is_server_backing_off() is True
    blocked_task = qm.get_next_task(priority_ceiling=2)
    assert blocked_task is None, "Очередь должна быть заблокирована во время backoff!"
    print(f"[OK] Очередь заблокирована до {qm.backing_off_until()} — новые задачи не выдаются")

    # Форсированно снимаем backoff (и глобальный, и по хосту), чтобы
    # продолжить тест (в реальной работе это время ожидания)
    kv_set(conn, "backing_off_until", "")
    conn.execute("DELETE FROM host_state")
    conn.commit()

    # 8. Эмуляция падения программы: берем задачу, "зависаем" в in_progress
    stuck_task = qm.get_next_task(priority_ceiling=2)
    assert stuck_task is not None and stuck_task.type == TYPE_IMAGE
    conn.close()  # имитируем внезапное завершение процесса без mark_done/mark_failed
    print(f"[OK] Задача {stuck_task.type} осталась в 'in_progress' — имитация падения программы")

    # "Перезапуск" программы: новое соединение с той же БД
    conn2 = connect_db(cfg.paths.db_path())
    qm2 = QueueManager(
        conn2,
        backoff_schedule=cfg.backoff.backoff_schedule_seconds,
        reset_streak_after_successes=cfg.backoff.reset_streak_after_successes,
        max_attempts_per_task=cfg.retry.max_attempts_per_task,
    )
    recovered = qm2.reset_stuck_in_progress()
    assert recovered == 1, f"Ожидалась 1 восстановленная задача, получено {recovered}"
    print(f"[OK] После 'перезапуска' восстановлено {recovered} зависшая задача (in_progress -> pending)")

    # 9. Снимок прогресса для UI
    final_counts = qm2.get_counts()
    write_progress_snapshot(cfg.paths.progress_json_path(), final_counts,
                             current_task={"type": "image", "url": "https://forum.test/images/photo1.jpg"})
    import os
    assert os.path.exists(cfg.paths.progress_json_path())
    print(f"[OK] progress_state.json записан: {cfg.paths.progress_json_path()}")

    print("\n=== Итоговая статистика ===")
    print(final_counts)

    print("\nВСЕ ПРОВЕРКИ ЭТАПА 1 ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
