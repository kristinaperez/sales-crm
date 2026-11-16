"""Подключение к базе. SQLite: WAL, внешние ключи, явный BEGIN.

Запросы, которые что-то меняют (всё, кроме GET), открывают транзакцию через BEGIN IMMEDIATE:
блокировка записи берётся сразу, и запросы разных пользователей выстраиваются в очередь.
Иначе SQLite отвечает «database is locked», когда транзакция сначала читает, а писать начинает
после того, как другой пользователь успел что-то записать.
"""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import config


def make_engine(url: str | None = None) -> Engine:
    url = url or config.DATABASE_URL
    is_sqlite = url.startswith("sqlite")
    kwargs: dict = {"future": True}
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
    else:
        kwargs["pool_pre_ping"] = True
    engine = create_engine(url, **kwargs)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record):  # noqa: ANN001
            # Транзакциями управляет SQLAlchemy (см. обработчик begin ниже):
            # так SAVEPOINT и блокировки SQLite работают предсказуемо.
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn):  # noqa: ANN001
            mode = conn.get_execution_options().get("sqlite_begin", "DEFERRED")
            conn.exec_driver_sql(f"BEGIN {mode}")

    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
# Тот же пул соединений, но транзакции начинаются сразу с блокировки записи.
_write_engine = engine.execution_options(sqlite_begin="IMMEDIATE") if engine.dialect.name == "sqlite" else engine
WriteSession = sessionmaker(bind=_write_engine, expire_on_commit=False, autoflush=False)


def get_db(request: Request) -> Iterator[Session]:
    db = SessionLocal() if request.method in ("GET", "HEAD") else WriteSession()
    try:
        yield db
    finally:
        db.close()
