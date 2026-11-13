"""app/db/url.py — выбор драйвера Postgres не зависит от написания DATABASE_URL (выполняется офлайн)."""
from __future__ import annotations

from app.db.url import normalize_database_url, redact_database_url


def test_plain_postgresql_scheme_gets_psycopg3_driver():
    assert normalize_database_url("postgresql://u:p@db:5432/x") == "postgresql+psycopg://u:p@db:5432/x"


def test_heroku_style_postgres_scheme_is_accepted_too():
    assert normalize_database_url("postgres://u:p@db/x") == "postgresql+psycopg://u:p@db/x"


def test_explicit_drivers_are_left_alone():
    assert normalize_database_url("postgresql+psycopg://u:p@db/x") == "postgresql+psycopg://u:p@db/x"
    assert normalize_database_url("postgresql+psycopg2://u:p@db/x") == "postgresql+psycopg2://u:p@db/x"


def test_non_postgres_urls_are_untouched():
    # тесты проекта идут на sqlite — его трогать нельзя
    assert normalize_database_url("sqlite://") == "sqlite://"
    assert normalize_database_url("sqlite:///tmp/x.db") == "sqlite:///tmp/x.db"


def test_whitespace_is_stripped_and_empty_stays_empty():
    assert normalize_database_url("  postgresql://u@h/x \n") == "postgresql+psycopg://u@h/x"
    assert normalize_database_url("") == ""
    assert normalize_database_url(None) == ""


def test_only_the_scheme_is_rewritten():
    # слово postgresql внутри пароля/пути не должно меняться
    url = "postgresql://u:postgresql://@h/db"
    assert normalize_database_url(url) == "postgresql+psycopg://u:postgresql://@h/db"


def test_redact_hides_password_only():
    assert redact_database_url("postgresql+psycopg://postgres:s3cret@db:5432/shelf") == \
        "postgresql+psycopg://postgres:***@db:5432/shelf"
    assert redact_database_url("sqlite://") == "sqlite://"
    assert redact_database_url("redis://redis:6379/0") == "redis://redis:6379/0"
