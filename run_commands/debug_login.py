"""
debug_login.py — диагностика проблем со входом на форум.

Показывает:
    1. Что реально прочиталось из .env (пароль частично скрыт, но видно
       длину и первый/последний символ — этого достаточно, чтобы заметить
       обрезание из-за "#" или лишние пробелы/кавычки).
    2. Какие скрытые поля формы логина были найдены и отправлены.
    3. Редиректы (например http -> https) и cookies на каждом шаге —
       частая причина ошибки "Ошибка отправки формы": cookie от GET
       не долетает до POST из-за смены протокола/домена при редиректе.
    4. Что ответил сервер после попытки входа — код ответа и фрагмент
       текста (там обычно видно причину: "неверный пароль", "слишком
       много попыток" и т.п.).

Запуск:
    py debug_login.py
"""


import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
# ^ путь к корню проекта (папка выше run_commands/) — нужен, чтобы
# "from utils...." и подобные внутрипроектные импорты работали
# независимо от того, откуда запущен этот файл (через меню main.py,
# или напрямую: py run_commands/run_archive.py).

import time

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from utils.config import load_config
from utils.credentials import load_credentials


def mask(value: str) -> str:
    if not value:
        return "(пусто)"
    if len(value) <= 2:
        return "*" * len(value)
    return value[0] + "*" * (len(value) - 2) + value[-1] + f"  (длина: {len(value)})"


def print_cookies(session: requests.Session, label: str):
    cookies = list(session.cookies)
    print(f"\nCookies в сессии {label} ({len(cookies)}):")
    if not cookies:
        print("  (пусто — это подозрительно, если ниже сайт использует сессии)")
    for c in cookies:
        print(f"  {c.name} = {c.value[:20]}{'...' if len(c.value) > 20 else ''}  "
              f"(domain={c.domain}, secure={c.secure})")


def print_redirect_chain(resp: requests.Response, label: str):
    print(f"\nЦепочка запросов при {label}:")
    for r in resp.history:
        print(f"  {r.status_code} {r.url}  ->")
    print(f"  {resp.status_code} {resp.url}  (итоговый адрес)")
    if resp.history:
        first_scheme = resp.history[0].url.split(':', 1)[0]
        last_scheme = resp.url.split(':', 1)[0]
        if first_scheme != last_scheme:
            print(f"  !! ВНИМАНИЕ: протокол сменился с {first_scheme} на {last_scheme} при редиректе.")
            print(f"     Если исходный адрес форума указан как '{first_scheme}://...' — поменяй на "
                  f"'{last_scheme}://...' в run_scan_test.py / config.json, это может решить проблему.")


def main():
    cfg = load_config()
    if not cfg.forum.base_url:
        base_url = input("Адрес форума: ").strip()
    else:
        base_url = cfg.forum.base_url
    print(f"Форум (из config.json): {base_url}")

    creds = load_credentials()
    print(f"\nПрочитано из .env:")
    print(f"  FORUM_USERNAME = {mask(creds.username or '')}")
    print(f"  FORUM_PASSWORD = {mask(creds.password or '')}")

    if not creds.is_set():
        print("\n.env пуст или не найден. Дальше продолжать нет смысла — сначала заполни .env.")
        return

    session = requests.Session()
    session.headers.update({"User-Agent": cfg.forum.user_agent})

    login_url = urljoin(base_url, "ucp.php?mode=login")
    print(f"\nЗагружаю страницу логина: {login_url}")
    try:
        get_resp = session.get(login_url, timeout=30, allow_redirects=True)
    except requests.exceptions.RequestException as e:
        print(f"\nОшибка сети при загрузке страницы логина: {e}")
        return
    print_redirect_chain(get_resp, "загрузке страницы логина (GET)")
    print_cookies(session, "после GET")

    if get_resp.status_code != 200:
        print(f"\nСервер вернул HTTP {get_resp.status_code} — дальше идти нет смысла.")
        return

    soup = BeautifulSoup(get_resp.text, "lxml")
    form = soup.find("form", id="login")
    if form is None:
        for f in soup.find_all("form"):
            if "mode=login" in f.get("action", ""):
                form = f
                break
    if form is None:
        print("\nФорма логина НЕ найдена на странице! Пришли этот HTML — поправим парсер.")
        return

    hidden_fields = {
        inp.get("name"): inp.get("value", "")
        for inp in form.find_all("input", attrs={"type": "hidden"})
        if inp.get("name")
    }
    print(f"\nНайдены скрытые поля формы ({len(hidden_fields)}):")
    for k, v in hidden_fields.items():
        print(f"  {k} = {v}")

    action = form.get("action") or "ucp.php?mode=login"
    post_url = urljoin(get_resp.url, action)

    print("\nЖду 2 секунды перед отправкой (phpBB отклоняет форму, если отправить "
          "её в ту же секунду, что и загрузили, — это особенность check_form_key)...")
    time.sleep(2)
    print(f"Отправляю логин на: {post_url}")

    payload = dict(hidden_fields)
    payload.update({
        "username": creds.username,
        "password": creds.password,
        "login": "Вход",  # значение как на реальной кнопке формы
    })

    post_resp = None
    try:
        post_resp = session.post(post_url, data=payload, timeout=30, allow_redirects=True)
    except requests.exceptions.RequestException as e:
        print(f"\nОшибка сети при отправке формы: {e}")
        return
    print_redirect_chain(post_resp, "отправке формы (POST)")
    print_cookies(session, "после POST")

    text = post_resp.text or ""
    logged_in = "mode=logout" in text
    print(f"\nСтатус ответа: HTTP {post_resp.status_code}")
    print(f"Признак успешного входа (ссылка 'mode=logout' на странице): {'ДА' if logged_in else 'НЕТ'}")

    error_soup = BeautifulSoup(text, "lxml")
    possible_error = error_soup.find(class_="error") or error_soup.find("p", class_="error")
    if possible_error:
        print(f"\nНайден блок с классом 'error' на странице:")
        print(f"  {possible_error.get_text(strip=True)}")

    if not logged_in:
        print(f"\nПервые 1500 символов ответа сервера:")
        print("-" * 70)
        print(text[:1500])
        print("-" * 70)


if __name__ == "__main__":
    main()

