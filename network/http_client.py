"""
http_client.py — низкоуровневый клиент для работы с phpBB-форумом.

Инкапсулирует:
    - requests.Session с cookies (для доступа к закрытым разделам форума
      нужен логин через ucp.php?mode=login — как обычный пользователь);
    - классификацию результата запроса (успех / отсутствует / ошибка сервера
      / истекла сессия), которую дальше использует Queue Manager для
      решения "повторить с backoff" или "пометить как отсутствующее";
    - задержку между запросами (throttle) — вызывается ПОСЛЕ успешного
      запроса, отдельно от самого fetch(), чтобы вызывающий код (Scanner,
      Downloader) явно управлял тем, когда делать паузу.

Важно: этот модуль НЕ содержит логики повторных попыток и backoff — это
ответственность QueueManager (см. database.py). Здесь только: выполнить
запрос один раз и честно сказать, что получилось.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from utils.logger_setup import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# Результат запроса
# ---------------------------------------------------------------------------

STATUS_SUCCESS = "success"
STATUS_MISSING = "missing"          # 404/410 — страницы не существует
STATUS_SERVER_ERROR = "server_error"  # 500/502/503/504/timeout/connection error
STATUS_AUTH_REQUIRED = "auth_required"  # сессия истекла / нет доступа к закрытому разделу
STATUS_CLIENT_ERROR = "client_error"    # прочие 4xx — не ошибка сервера, но и не 200


@dataclass
class FetchResult:
    status: str
    http_status: Optional[int] = None
    text: Optional[str] = None
    final_url: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class BinaryFetchResult:
    status: str
    http_status: Optional[int] = None
    content: Optional[bytes] = None
    content_type: Optional[str] = None
    error_message: Optional[str] = None


SERVER_ERROR_CODES = {500, 502, 503, 504}
MISSING_CODES = {404, 410}


class LoginError(Exception):
    pass


class ForumClient:
    """
    Обертка над requests.Session для последовательной, аккуратной работы
    с форумом: один запрос за раз, без параллелизма, с логином при
    необходимости.
    """

    def __init__(self, base_url: str, user_agent: str,
                 min_delay: float = 20.0, max_delay: float = 40.0,
                 timeout: int = 30):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self._logged_in = False

    # -- throttle ----------------------------------------------------------

    def throttle_delay(self) -> float:
        """
        Случайная пауза в заданном диапазоне. Вызывается вызывающим кодом
        ПОСЛЕ успешного запроса, перед следующим запросом — как того
        требует ТЗ (п.1: приоритет — минимальная нагрузка на сервер).
        """
        delay = random.uniform(self.min_delay, self.max_delay)
        logger.debug(f"Пауза {delay:.1f} сек. перед следующим запросом")
        time.sleep(delay)
        return delay

    # -- низкоуровневый запрос ----------------------------------------------

    def _raw_request(self, method: str, url: str, data: Optional[dict] = None,
                      skip_login_page_check: bool = False) -> FetchResult:
        try:
            resp = self.session.request(
                method, url, data=data, timeout=self.timeout, allow_redirects=True
            )
        except requests.exceptions.Timeout:
            return FetchResult(status=STATUS_SERVER_ERROR, error_message="Timeout")
        except requests.exceptions.ConnectionError as e:
            return FetchResult(status=STATUS_SERVER_ERROR, error_message=f"Connection error: {e}")
        except requests.exceptions.RequestException as e:
            return FetchResult(status=STATUS_SERVER_ERROR, error_message=f"Request error: {e}")

        code = resp.status_code

        if code in SERVER_ERROR_CODES:
            return FetchResult(status=STATUS_SERVER_ERROR, http_status=code,
                                error_message=f"HTTP {code}")
        if code in MISSING_CODES:
            return FetchResult(status=STATUS_MISSING, http_status=code,
                                error_message=f"HTTP {code}")
        if code == 403:
            # Часто означает "нет доступа к закрытому разделу" — не обязательно
            # истекшая сессия, но обрабатываем через тот же канал auth_required,
            # чтобы вызывающий код мог решить, пробовать ли перелогиниться.
            return FetchResult(status=STATUS_AUTH_REQUIRED, http_status=code,
                                text=resp.text, final_url=resp.url,
                                error_message="HTTP 403 Forbidden")
        if 400 <= code < 500:
            return FetchResult(status=STATUS_CLIENT_ERROR, http_status=code,
                                error_message=f"HTTP {code}")
        if not (200 <= code < 300):
            # На всякий случай — необработанные коды считаем ошибкой сервера,
            # чтобы не потерять данные молча.
            return FetchResult(status=STATUS_SERVER_ERROR, http_status=code,
                                error_message=f"Unexpected HTTP {code}")

        text = resp.text
        if not skip_login_page_check and self._looks_like_login_page(text):
            return FetchResult(status=STATUS_AUTH_REQUIRED, http_status=code,
                                text=text, final_url=resp.url,
                                error_message="Сессия истекла / требуется вход")

        return FetchResult(status=STATUS_SUCCESS, http_status=code, text=text, final_url=resp.url)

    def fetch(self, url: str) -> FetchResult:
        """Выполняет один GET-запрос и классифицирует результат."""
        return self._raw_request("GET", url)

    def fetch_binary(self, url: str) -> BinaryFetchResult:
        """
        Скачивает бинарный ресурс (изображение, вложение, аватар, CSS-файл).
        Отдельно от fetch(), потому что бинарные данные нельзя декодировать
        как текст (resp.text испортил бы содержимое), и потому что здесь не
        нужна проверка "похоже на страницу логина" — это не HTML.

        Отправляет заголовок Referer = адрес форума. Без него сторонние
        фотохостинги, на которые пользователи форума вставляли картинки
        хотлинком (radikal.ru, postimages.org и подобные — встречаются на
        реальных страницах архивируемого форума), часто отвечают 403 —
        это защита от хотлинкинга у НИХ, а не что-то про вход на форум,
        но без заголовка не отличить одно от другого. Если ссылка на
        самом деле мертва (сервис закрылся или картинку удалили) — Referer,
        конечно, не поможет, тут уже ничего не сделать.
        """
        try:
            resp = self.session.get(
                url, timeout=self.timeout, allow_redirects=True,
                headers={"Referer": self.base_url},
            )
        except requests.exceptions.Timeout:
            return BinaryFetchResult(status=STATUS_SERVER_ERROR, error_message="Timeout")
        except requests.exceptions.ConnectionError as e:
            return BinaryFetchResult(status=STATUS_SERVER_ERROR, error_message=f"Connection error: {e}")
        except requests.exceptions.RequestException as e:
            return BinaryFetchResult(status=STATUS_SERVER_ERROR, error_message=f"Request error: {e}")

        code = resp.status_code

        if code in SERVER_ERROR_CODES:
            return BinaryFetchResult(status=STATUS_SERVER_ERROR, http_status=code,
                                      error_message=f"HTTP {code}")
        if code in MISSING_CODES:
            return BinaryFetchResult(status=STATUS_MISSING, http_status=code,
                                      error_message=f"HTTP {code}")
        if code == 403:
            return BinaryFetchResult(status=STATUS_AUTH_REQUIRED, http_status=code,
                                      error_message="HTTP 403 Forbidden")
        if 400 <= code < 500:
            return BinaryFetchResult(status=STATUS_CLIENT_ERROR, http_status=code,
                                      error_message=f"HTTP {code}")
        if not (200 <= code < 300):
            return BinaryFetchResult(status=STATUS_SERVER_ERROR, http_status=code,
                                      error_message=f"Unexpected HTTP {code}")

        content_type = resp.headers.get("Content-Type", "")
        # Если сервер вместо картинки/вложения тихо вернул HTML (например,
        # ту же форму логина при истекшей сессии) — это тоже auth_required,
        # даже без явного 403.
        if "text/html" in content_type.lower():
            return BinaryFetchResult(status=STATUS_AUTH_REQUIRED, http_status=code,
                                      error_message="Получен HTML вместо файла — вероятно, требуется вход")

        return BinaryFetchResult(status=STATUS_SUCCESS, http_status=code,
                                  content=resp.content, content_type=content_type)

    # -- логин ---------------------------------------------------------------

    @staticmethod
    def _looks_like_login_page(html: str) -> bool:
        """
        Эвристика: страница похожа на форму логина phpBB, если в ней есть
        поле username/password формы с mode=login. Используется для
        обнаружения "вылета" из закрытого раздела в форму логина (сессия
        истекла) даже когда сервер вернул 200 OK.
        """
        if "mode=login" not in html:
            return False
        return bool(re.search(r'name=["\']username["\']', html)) and \
            bool(re.search(r'name=["\']password["\']', html))

    def is_logged_in(self) -> bool:
        return self._logged_in

    def login(self, username: str, password: str) -> bool:
        """
        Логин как обычный пользователь через ucp.php?mode=login.

        phpBB защищает форму логина anti-CSRF полями (creation_time,
        form_token, sid) — их нужно сначала получить с формы, затем
        отправить вместе с учетными данными.
        """
        login_url = urljoin(self.base_url, "ucp.php?mode=login")
        get_result = self._raw_request("GET", login_url, skip_login_page_check=True)
        if get_result.status != STATUS_SUCCESS or not get_result.text:
            raise LoginError(f"Не удалось загрузить страницу логина: {get_result.error_message}")

        soup = BeautifulSoup(get_result.text, "lxml")
        form = soup.find("form", id="login") or self._find_login_form(soup)
        if form is None:
            raise LoginError("На странице не найдена форма логина (возможно, изменена структура темы)")

        hidden_fields = {
            inp.get("name"): inp.get("value", "")
            for inp in form.find_all("input", attrs={"type": "hidden"})
            if inp.get("name")
        }

        action = form.get("action") or "ucp.php?mode=login"
        post_url = urljoin(get_result.final_url or login_url, action)

        # phpBB отклоняет форму, если она отправлена в ту же секунду, что и
        # загружена (check_form_key: при diff=0 между creation_time и текущим
        # временем проверка не проходит, так как 0 трактуется как "ложь").
        # Пауза с запасом решает это для скриптовой отправки.
        time.sleep(2)

        payload = dict(hidden_fields)
        payload.update({
            "username": username,
            "password": password,
            "login": "Login",
        })

        post_result = self._raw_request("POST", post_url, data=payload, skip_login_page_check=True)
        if post_result.http_status is not None and post_result.http_status in SERVER_ERROR_CODES:
            raise LoginError(f"Ошибка сервера при отправке формы логина: {post_result.error_message}")
        if not post_result.text:
            raise LoginError(f"Пустой ответ при отправке формы логина: {post_result.error_message}")

        if self._login_succeeded(post_result.text):
            self._logged_in = True
            logger.info("Вход на форум выполнен успешно.")
            return True

        logger.error("Вход не выполнен — проверьте логин/пароль или структуру формы (custom-тема).")
        self._logged_in = False
        return False

    @staticmethod
    def _find_login_form(soup: BeautifulSoup):
        for form in soup.find_all("form"):
            action = form.get("action", "")
            if "mode=login" in action:
                return form
        return None

    @staticmethod
    def _login_succeeded(html: str) -> bool:
        """
        Признак успешного входа: на странице есть ссылка на выход
        (mode=logout) — она появляется в шапке форума только у
        авторизованных пользователей.
        """
        return bool(re.search(r'mode=logout', html))

    def login_forum2x2(self, username: str, password: str) -> bool:
        """
        Логин для forum2x2 — форма POST /login.

        Подтверждено на реальной странице входа (скрипт_входа.txt):
        форма НАМНОГО проще, чем у phpBB — нет анти-CSRF токена
        (creation_time/form_token/sid не встречаются вообще), поэтому не
        нужен ни предварительный GET за токеном, ни пауза перед отправкой —
        можно просто отправить POST сразу. Поля формы: username, password,
        autologin (чекбокс, в разметке стоит checked по умолчанию — шлём
        "on"), redirect и query (скрытые, пустые в разметке), кнопка
        login="Вход".

        Признак УСПЕШНОГО входа ПОДТВЕРЖДЁН на реальной странице (после_входа.html):
        в шапке появляется настоящая ссылка на выход
        href="/login?logout=1&tid=...&key=..." с id="logout" — точно та же
        идея, что у phpBB с mode=logout, только другой формат ссылки.
        Раньше тут была осторожная эвристика "формы входа больше нет" —
        она хоть и работала бы верно на этой конкретной странице, но
        могла в принципе ошибиться (например, на промежуточной странице
        без формы, но и без входа), поэтому заменена на прямую проверку
        logout=1.
        """
        login_url = urljoin(self.base_url, "login")
        payload = {
            "username": username,
            "password": password,
            "autologin": "on",
            "redirect": "",
            "query": "",
            "login": "Вход",
        }
        post_result = self._raw_request("POST", login_url, data=payload, skip_login_page_check=True)
        if post_result.http_status is not None and post_result.http_status in SERVER_ERROR_CODES:
            raise LoginError(f"Ошибка сервера при отправке формы входа: {post_result.error_message}")
        if not post_result.text:
            raise LoginError(f"Пустой ответ при отправке формы входа: {post_result.error_message}")

        if "logout=1" in post_result.text:
            self._logged_in = True
            logger.info("Вход на forum2x2 выполнен успешно (найдена ссылка на выход logout=1).")
            return True

        logger.error("Вход на forum2x2 не выполнен — проверьте логин/пароль в .env.forum2x2.")
        self._logged_in = False
        return False
