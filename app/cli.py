"""Служебные команды: python -m app.cli <команда>.

  init            миграции уже применены — создать руководителя и готовую воронку
  seed-demo       добавить демоданные (только в пустую базу)
  create-user     создать сотрудника: create-user ЛОГИН "Имя Фамилия" ПАРОЛЬ [head|manager]
  set-password    сменить пароль: set-password ЛОГИН ПАРОЛЬ
  backup [папка]  резервная копия всей базы SQLite (по умолчанию ./backups рядом с базой)
  restore ФАЙЛ    восстановить базу из копии (приложение должно быть остановлено)
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from . import config
from .db import SessionLocal
from .models import User
from .security import hash_password
from .services import setup


def cmd_init() -> None:
    with SessionLocal() as db:
        setup.ensure_default_pipeline(db)
        head, password = setup.ensure_head(db)
        db.commit()
        if password:
            print("=" * 64)
            print("Создан руководитель.")
            print(f"  Логин:  {head.login}")
            print(f"  Пароль: {password}")
            print("Пароль сгенерирован, потому что ADMIN_PASSWORD не задан. Смените его после входа.")
            print("=" * 64, flush=True)


def cmd_seed_demo() -> None:
    with SessionLocal() as db:
        if setup.seed_demo(db):
            print(f"Демоданные добавлены. Менеджеры: anna и boris, пароль {setup.DEMO_PASSWORD}", flush=True)
        else:
            print("В базе уже есть компании — демоданные не добавлены.", flush=True)


def cmd_create_user(login: str, name: str, password: str, role: str = "manager") -> None:
    with SessionLocal() as db:
        try:
            setup.create_user(db, login, name, password, role)
        except setup.Invalid as exc:
            sys.exit(str(exc))
        db.commit()
        print(f"Сотрудник {login} создан.")


def cmd_set_password(login: str, password: str) -> None:
    if len(password) < 8:
        sys.exit("Пароль должен быть не короче 8 символов.")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.login == login.lower()))
        if user is None:
            sys.exit("Такого логина нет.")
        user.password_hash = hash_password(password)
        db.commit()
        print("Пароль изменён.")


def _db_path() -> Path:
    path = config.sqlite_path()
    if path is None:
        sys.exit("Команда работает только с SQLite. Для PostgreSQL используйте pg_dump / pg_restore.")
    return path


def cmd_backup(target_dir: str | None = None) -> None:
    """Согласованная копия через SQLite Online Backup API — можно делать при работающем приложении."""
    src_path = _db_path()
    out_dir = Path(target_dir) if target_dir else src_path.parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"crm-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if check != "ok":
        out.unlink(missing_ok=True)
        sys.exit(f"Копия не прошла проверку целостности: {check}")
    print(f"Резервная копия: {out} ({out.stat().st_size // 1024} КБ), проверка целостности пройдена.")


def cmd_restore(source: str) -> None:
    src = Path(source)
    if not src.is_file():
        sys.exit(f"Файл не найден: {src}")
    probe = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            sys.exit("Файл копии повреждён — восстановление отменено.")
        if not probe.execute("SELECT name FROM sqlite_master WHERE name='companies'").fetchone():
            sys.exit("Это не похоже на базу CRM — восстановление отменено.")
    finally:
        probe.close()
    dst = _db_path()
    if dst.exists():
        keep = dst.with_name(f"{dst.name}.before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(dst, keep)
        print(f"Текущая база сохранена как {keep}")
    for suffix in ("-wal", "-shm"):
        Path(str(dst) + suffix).unlink(missing_ok=True)
    shutil.copy2(src, dst)
    print(f"База восстановлена из {src}. Запустите приложение — недостающие миграции применятся сами.")


COMMANDS = {
    "init": cmd_init, "seed-demo": cmd_seed_demo, "create-user": cmd_create_user,
    "set-password": cmd_set_password, "backup": cmd_backup, "restore": cmd_restore,
}


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in COMMANDS:
        sys.exit(__doc__)
    try:
        COMMANDS[argv[0]](*argv[1:])
    except TypeError:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
