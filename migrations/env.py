"""Окружение Alembic. Подключение к базе — по порядку:
  1. готовое соединение, переданное кодом (app/db/migrate.py кладёт его в config.attributes["connection"]) — так bootstrap и тесты
     применяют миграции под своей блокировкой и в своей базе;
  2. адрес из командной строки: alembic -x url=postgresql://… upgrade head;
  3. DATABASE_URL из настроек приложения (как у api и worker).
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.db.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    from app.db.url import normalize_database_url
    url = context.get_x_argument(as_dictionary=True).get("url")
    if not url:
        from app.core.config import get_settings
        url = get_settings().database_url
    return normalize_database_url(url)


def _configure(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                      render_as_batch=connection.dialect.name == "sqlite")   # SQLite меняет таблицы только пересозданием


def run_migrations_offline() -> None:
    """alembic upgrade head --sql: печать SQL без подключения к базе (для просмотра перед применением)."""
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
