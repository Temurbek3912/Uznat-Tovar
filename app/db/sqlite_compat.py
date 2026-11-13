"""
SQLite ведёт себя как PostgreSQL там, где на это опирается код (тесты, будущий Desktop при выборе SQLite по D-02).

Встроенная lower() в SQLite меняет регистр только латиницы: «КОЛА» не находилась по «кола», и поиск товаров (app/core/text_search.py)
на SQLite не работал для кириллицы. Подменяем lower() на Unicode-версию Python — тот же результат, что lower() в PostgreSQL с UTF-8.
Подключается один раз при импорте app.db (слушатель на все движки; для других баз ничего не делает).
"""
from __future__ import annotations

import sqlite3

from sqlalchemy import event
from sqlalchemy.engine import Engine


def _unicode_lower(value):
    return value.lower() if isinstance(value, str) else value


@event.listens_for(Engine, "connect")
def _on_connect(dbapi_connection, _record) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function("lower", 1, _unicode_lower, deterministic=True)
