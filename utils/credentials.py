"""
credentials.py — загрузка логина/пароля форума из .env (никогда из кода
или config.json — пароль не должен лежать в открытом виде в файле,
который может случайно попасть в git).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


@dataclass
class ForumCredentials:
    username: Optional[str]
    password: Optional[str]

    def is_set(self) -> bool:
        return bool(self.username and self.password)


def load_credentials(env_path: str = ".env", prefix: str = "FORUM") -> ForumCredentials:
    """
    env_path — какой .env-файл читать (у каждого форума свой, чтобы логины
    и пароли разных форумов не путались и не перетирали друг друга).
    prefix — префикс переменных внутри этого файла: по умолчанию
    FORUM_USERNAME/FORUM_PASSWORD (первый форум); для второго форума
    (другой движок, другие данные для входа) используется свой префикс,
    например FORUM2X2_USERNAME/FORUM2X2_PASSWORD в отдельном .env.forum2x2.

    override=True — обязательно, иначе если в процессе уже была вызвана
    load_credentials() для одного форума, повторный вызов для другого
    форума с ДРУГИМ env_path, но случайно теми же именами переменных,
    вернул бы старые значения (python-dotenv по умолчанию не перезаписывает
    уже установленные переменные окружения). Это станет актуально, когда
    на этапе 6 в одном интерфейсе можно будет переключаться между форумами.
    """
    load_dotenv(env_path, override=True)
    return ForumCredentials(
        username=os.getenv(f"{prefix}_USERNAME") or None,
        password=os.getenv(f"{prefix}_PASSWORD") or None,
    )
