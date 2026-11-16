"""Настройки приложения. Всё задаётся переменными окружения."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent


def _default_database_url() -> str:
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    return f"sqlite:///{data_dir / 'crm.db'}"


DATABASE_URL: str = os.environ.get("DATABASE_URL") or _default_database_url()
APP_TZ_NAME: str = os.environ.get("APP_TZ", "Europe/Moscow")
APP_TZ = ZoneInfo(APP_TZ_NAME)
SESSION_HTTPS_ONLY: bool = os.environ.get("SESSION_HTTPS_ONLY", "0") == "1"
CURRENCY: str = os.environ.get("CURRENCY", "₽")
PAGE_SIZE = 50
PREVIEW_PAGE_SIZE = 50
KANBAN_COLUMN_LIMIT = 40
MAX_IMPORT_ROWS = 10_000
MAX_IMPORT_BYTES = 2 * 1024 * 1024  # 10 000 строк по 200 символов


def sqlite_path() -> Path | None:
    """Путь к файлу SQLite или None, если база не SQLite."""
    if not DATABASE_URL.startswith("sqlite"):
        return None
    raw = DATABASE_URL.split("///", 1)[1] if "///" in DATABASE_URL else ""
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def secret_key() -> str:
    """SECRET_KEY из окружения; иначе создаётся один раз и хранится рядом с базой."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    db_path = sqlite_path()
    key_file = (db_path.parent if db_path else BASE_DIR / "data") / "secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    key_file.write_text(key)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key
