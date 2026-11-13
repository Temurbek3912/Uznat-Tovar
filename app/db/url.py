"""
Нормализация DATABASE_URL. Только stdlib — модуль тестируется офлайн (tests/test_database_url.py).

Зачем. SQLAlchemy выбирает драйвер Postgres по схеме URL, а сам код проекта ни `psycopg`, ни `psycopg2` не
импортирует. Поэтому «нет пакета psycopg» всплывало только в момент первого подключения к БД, и только если
в URL стояло `postgresql+psycopg://`, а в requirements.txt был лишь psycopg2-binary (или наоборот).
Теперь схема приводится к одному виду в одном месте, а в requirements.txt стоят оба драйвера:

    postgres://…, postgresql://…   ->  postgresql+psycopg://…   (psycopg 3 — основной драйвер)
    postgresql+psycopg://…         ->  без изменений
    postgresql+psycopg2://…        ->  без изменений (явный выбор старого драйвера, он тоже установлен)
    sqlite://…, всё остальное      ->  без изменений (тесты используют sqlite)
"""
from __future__ import annotations

import re

_PLAIN_POSTGRES_PREFIXES = ("postgresql://", "postgres://")
_PASSWORD_RE = re.compile(r"(://[^:/@\s]+:)([^@\s]*)(@)")


def normalize_database_url(url: str) -> str:
    """URL из настроек -> URL с явным драйвером psycopg 3 (если драйвер не указан)."""
    cleaned = (url or "").strip()
    for prefix in _PLAIN_POSTGRES_PREFIXES:
        if cleaned.startswith(prefix):
            return "postgresql+psycopg://" + cleaned[len(prefix):]
    return cleaned


def redact_database_url(url: str) -> str:
    """URL без пароля — для логов и сообщений об ошибках."""
    return _PASSWORD_RE.sub(r"\1***\3", url or "")
