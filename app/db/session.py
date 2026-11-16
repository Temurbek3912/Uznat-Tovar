from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.url import normalize_database_url

SQLITE_POOL = {"pool_size": 20, "max_overflow": 80}

_settings = get_settings()
# normalize_database_url: явный драйвер psycopg 3 (см. app/db/url.py), чтобы выбор драйвера не зависел от написания URL.
_url = normalize_database_url(_settings.database_url)
# Файл SQLite (облегчённый режим): соединения дешёвые, а стандартных 5 + 10 не хватало — при 100 покупателях одновременно
# список товаров падал с ошибкой 500 («QueuePool limit ... reached», замер 2026-11-16). Запросов в обработке одновременно не больше
# числа потоков сервера (40) плюс задачи распознавания, поэтому 100 — с запасом.
_pool = SQLITE_POOL if _url.startswith("sqlite") and ":memory:" not in _url else {}
engine = create_engine(_url, pool_pre_ping=True, **_pool)

if engine.dialect.name == "sqlite" and _settings.lite_mode:
    # облегчённый режим (документ 11, §10): файл SQLite. WAL — чтение не ждёт записи; busy_timeout — короткая запись другого
    # потока подождёт, а не упадёт с «database is locked».
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI-зависимость: одна сессия на запрос, гарантированное закрытие."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
