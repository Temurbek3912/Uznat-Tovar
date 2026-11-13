"""Пакет базы данных. sqlite_compat подключается здесь, чтобы любой движок SQLite (тесты, Desktop) искал без учёта регистра кириллицы."""
try:
    from app.db import sqlite_compat  # noqa: F401
except ImportError:          # без SQLAlchemy (часть офлайн-проверок) пакет по-прежнему импортируется
    pass
