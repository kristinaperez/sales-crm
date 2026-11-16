"""Миграции схемы. База задаётся переменной DATABASE_URL — та же, что у приложения."""
from logging.config import fileConfig

from alembic import context

from app import config as app_config
from app.db import make_engine
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=app_config.DATABASE_URL, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(app_config.DATABASE_URL)
    with engine.connect() as connection:
        # render_as_batch нужен SQLite для ALTER TABLE; в PostgreSQL он ничего не меняет.
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
