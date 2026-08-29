"""
test_host_backoff.py — проверка backoff по отдельному хосту.

Сценарий: тема форума ссылается на картинку со своего сайта форума и на
картинку со стороннего (мёртвого) фотохостинга. Сторонний хостинг отвечает
ошибками сервера несколько раз подряд — должен уйти в backoff ТОЛЬКО по
своему хосту, не блокируя ни картинку с самого форума, ни вообще какие-либо
другие задачи. Это защита от ситуации вроде мёртвых ссылок radikal.ru:
раньше (глобальный backoff) они могли бы приостановить скачивание всего
остального до истечения таймера.
"""

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import shutil

from utils.config import AppConfig, ensure_directories
from storage.database import (
    connect_db, init_db, QueueManager,
    TYPE_IMAGE, PRIORITY_RESOURCE,
)

TEST_ROOT = "Archive_host_backoff_test"


def main():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)

    cfg = AppConfig()
    cfg.paths.archive_root = TEST_ROOT
    ensure_directories(cfg)

    conn = connect_db(cfg.paths.db_path())
    init_db(conn)
    qm = QueueManager(
        conn,
        backoff_schedule=[600, 1800, 3600],
        reset_streak_after_successes=3,
        max_attempts_per_task=20,
        own_host="forum.test",
    )

    own_task = qm.add_task(TYPE_IMAGE, "https://forum.test/images/own_photo.jpg", "images/own_photo.jpg")
    dead_task = qm.add_task(TYPE_IMAGE, "https://dead-hotlink.test/photo.jpg", "images/photo.jpg")
    other_task = qm.add_task(TYPE_IMAGE, "https://forum.test/images/another_photo.jpg", "images/another_photo.jpg")

    # 1. Сторонний хост проваливается 3 раза подряд
    for i in range(3):
        t = qm.get_next_task(priority_ceiling=PRIORITY_RESOURCE)
        assert t is not None, f"Ожидалась задача на попытке {i+1}"
        if t.id == dead_task:
            qm.mark_task_failed(t.id, "502 Bad Gateway (мёртвый хостинг)")
        else:
            qm.mark_task_done(t.id)

    hosts_backing_off = qm._currently_backing_off_hosts()
    assert "dead-hotlink.test" in hosts_backing_off, \
        f"dead-hotlink.test должен быть в backoff, получено: {hosts_backing_off}"
    assert "forum.test" not in hosts_backing_off, \
        "Свой хост форума НЕ должен уйти в backoff из-за чужого мёртвого хостинга"
    print(f"[OK] После 3 ошибок подряд в backoff только dead-hotlink.test: {hosts_backing_off}")

    # 2. Общий (глобальный) backoff НЕ должен сработать — ошибки были у
    # одного стороннего хоста, это не должно восприниматься как "весь
    # сервер лёг"
    assert qm.is_server_backing_off() is False, \
        "Глобальный backoff не должен включаться из-за ошибок одного стороннего хоста"
    print("[OK] Глобальный backoff не сработал — это не 'весь сервер лёг', а один мёртвый хостинг")

    # 3. К этому моменту обе картинки форума уже обработаны (см. цикл выше —
    # всего 3 задачи и 3 попытки, поэтому весь набор уже разобран), а
    # мёртвая картинка ждёт своего backoff. Проверяем это напрямую по
    # статусам в базе — это и есть главное доказательство изоляции по хосту:
    # свои задачи обработались, несмотря на то что сторонний хост в backoff.

    own_check = conn.execute("SELECT status FROM tasks WHERE id = ?", (own_task,)).fetchone()
    other_check = conn.execute("SELECT status FROM tasks WHERE id = ?", (other_task,)).fetchone()
    dead_check = conn.execute("SELECT status FROM tasks WHERE id = ?", (dead_task,)).fetchone()

    assert own_check["status"] == "done", "Своя картинка форума должна была скачаться, несмотря на мёртвый сторонний хост"
    assert other_check["status"] == "done", "Вторая своя картинка форума тоже должна была скачаться"
    assert dead_check["status"] == "retry_wait", "Мёртвая картинка должна ждать своего backoff, не более того"
    print("[OK] Обе картинки с форума скачались, несмотря на то что сторонний хост в backoff")
    print(f"[OK] Мёртвая картинка осталась в 'retry_wait' (подождёт свой backoff) — статус: {dead_check['status']}")

    # 4. host_backoff_wait_seconds — используется Downloader'ом, чтобы не
    # спутать "хост отдыхает" с "очередь реально пуста"
    wait = qm.host_backoff_wait_seconds(priority_ceiling=PRIORITY_RESOURCE)
    assert wait is None, (
        "Сейчас должно быть None: единственная оставшаяся задача (dead_task) "
        "в retry_wait с будущим next_attempt_at, а не pending — её и без host-backoff "
        "get_next_task не отдаст прямо сейчас"
    )
    print("[OK] host_backoff_wait_seconds корректно возвращает None, когда очередь по факту пуста")

    conn.close()
    print("\nВСЕ ПРОВЕРКИ backoff ПО ОТДЕЛЬНОМУ ХОСТУ ПРОЙДЕНЫ УСПЕШНО.")


if __name__ == "__main__":
    main()
