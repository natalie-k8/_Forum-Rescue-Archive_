"""
site_registry.py — реестр отдельных форумов на одном движке.

Нужен для случаев вроде forum2x2.ru: это не один форум, а хостинг, на
котором может жить сколько угодно НЕСВЯЗАННЫХ друг с другом форумов —
у каждого свой адрес, своя структура, обычно свой логин. Раньше
run_forum2x2_archive.py был жёстко привязан к ОДНОМУ файлу конфигурации
(config_forum2x2.json) и ОДНОЙ папке архива (Archive_forum2x2/) — второй
форум на том же движке просто перезаписал бы первый.

Этот модуль хранит соответствие "адрес форума → своя папка архива и свой
файл конфигурации" в простом JSON-реестре рядом с остальными файлами
проекта. Для НОВОГО адреса папка и файл конфигурации создаются
автоматически, с именем на основе хоста (например,
Archive_forum2x2_example.ru/) — руками ничего называть не нужно.
Для уже известного адреса реестр просто возвращает те же самые пути, что
и раньше, чтобы прогресс не терялся.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlparse


@dataclass
class SiteEntry:
    base_url: str
    config_path: str
    archive_root: str
    credentials_path: str
    env_path: str    # <-- ДОБАВИТЬ ЭТУ СТРОКУ


def _sanitize_for_filesystem(text: str) -> str:
    """Оставляет буквы/цифры/точки/дефисы/подчёркивания — безопасно и для
    имени файла, и для имени папки на Windows."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "site"


def hostname_key(base_url: str) -> str:
    """Хост из URL, без порта — используется как ключ реестра и как часть
    имени папки/файла (например, 'example.ru')."""
    netloc = urlparse(base_url).netloc.lower()
    host = netloc.split(":")[0]
    return _sanitize_for_filesystem(host) if host else "site"


def load_registry(registry_path: str) -> dict[str, SiteEntry]:
    """
    Записи, сохранённые до появления поля env_path в SiteEntry, в JSON
    его не содержат — подставляем credentials_path как запасной вариант,
    чтобы старый реестр продолжал загружаться без ручной правки файла.
    """
    if not os.path.exists(registry_path):
        return {}
    with open(registry_path, encoding="utf-8") as f:
        raw = json.load(f)
    result = {}
    for key, value in raw.items():
        value.setdefault("env_path", value.get("credentials_path", ""))
        result[key] = SiteEntry(**value)
    return result


def save_registry(registry_path: str, registry: dict[str, SiteEntry]) -> None:
    raw = {key: asdict(value) for key, value in registry.items()}
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def get_or_create_site(
    registry_path: str,
    base_url: str,
    config_prefix: str,
    archive_prefix: str,
    credentials_prefix: str = ".env.forum2x2",
) -> tuple[SiteEntry, bool]:
    """
    Возвращает (SiteEntry, is_new) для данного адреса форума.

    Если такой хост уже есть в реестре — возвращает существующие пути
    (config_path/archive_root/credentials_path) без изменений, is_new=False.
    Если это новый хост — создаёт новую запись с именами файла/папки на
    основе хоста, сохраняет реестр, is_new=True. Сами файл конфигурации,
    папка архива и .env физически создаются позже (config/архив —
    обычным ensure_directories(), .env — пользователем вручную) — здесь
    только выделяются ИМЕНА для них.

    credentials_path — СВОЙ файл .env на каждый форум (например,
    ".env.forum2x2_example.ru"), а не общий: на одном хостинге
    forum2x2 разные форумы почти всегда требуют разные логин/пароль.
    """
    registry = load_registry(registry_path)
    key = hostname_key(base_url)

    if key in registry:
        return registry[key], False

    entry = SiteEntry(
        base_url=base_url,
        config_path=f"{config_prefix}_{key}.json",
        archive_root=f"{archive_prefix}_{key}",
        credentials_path=credentials_path,
        env_path=credentials_path,
    )
    registry[key] = entry
    save_registry(registry_path, registry)
    return entry, True


def list_sites(registry_path: str) -> list[SiteEntry]:
    return list(load_registry(registry_path).values())


def seed_legacy_site(
    registry_path: str,
    legacy_config_path: str,
    legacy_archive_root: str,
    legacy_credentials_path: str,
) -> None:
    """
    Разово "заселяет" реестр уже существующим форумом — если до появления
    реестра уже был в работе config.json/Archive/ с реальными данными,
    их НЕЛЬЗЯ просто переименовать в новую схему (осиротеет архив). Если
    реестра ещё нет, а старый config.json уже существует и в нём есть
    base_url — добавляет запись, указывающую на СТАРЫЕ пути как есть
    (config.json/Archive/.env), без переименования файлов на диске.
    Если реестр уже существует (хотя бы раз запускались после этой
    доработки) — ничего не делает, чтобы не перезаписать выбор
    пользователя.
    """
    if os.path.exists(registry_path):
        return
    if not os.path.exists(legacy_config_path):
        return

    try:
        with open(legacy_config_path, encoding="utf-8") as f:
            data = json.load(f)
        base_url = data.get("forum", {}).get("base_url", "")
    except (json.JSONDecodeError, OSError):
        return

    if not base_url:
        return

    key = hostname_key(base_url)
    registry = {key: SiteEntry(
        base_url=base_url,
        config_path=legacy_config_path,
        archive_root=legacy_archive_root,
        credentials_path=legacy_credentials_path,
    )}
    save_registry(registry_path, registry)
