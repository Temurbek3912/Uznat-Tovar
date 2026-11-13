from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.url import normalize_database_url

_settings = get_settings()
# normalize_database_url: явный драйвер psycopg 3 (см. app/db/url.py), чтобы выбор драйвера не зависел от написания URL.
engine = create_engine(normalize_database_url(_settings.database_url), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI-зависимость: одна сессия на запрос, гарантированное закрытие."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
