"""
Поиск товаров без учёта регистра и вариантов написания (app/core/text_search.py).

Чистая часть — всегда. Часть на настоящем PostgreSQL — то же SQL-выражение, что строят маршруты, выполняется через psql на базе
из FULL_BACKUP_TEST_DB_URL (временный кластер; без переменной — пропуск). SQLAlchemy в облачной среде нет, поэтому выражение
маршрутов и текст для psql строятся из одного списка замен (REPLACEMENTS) — расхождения быть не может.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

from app.core import text_search as ts


@pytest.mark.parametrize("raw,expected", [
    ("ЁЛКА", "елка"), ("  Coca   Cola ", "coca cola"), ("Oʻzbekiston", "o'zbekiston"), ("Oʼzbek", "o'zbek"), ("O‘zbek", "o'zbek"),
    ("O`zbek", "o'zbek"), ("МОЛОКО «Простоквашино»", "молоко «простоквашино»"), ("", ""), (None, ""),
    ("е\u0308лка", "елка"),                                   # «ё», набранное как «е» + знак: NFC -> ё -> е
])
def test_normalize(raw, expected):
    assert ts.normalize(raw) == expected


def test_like_pattern_escapes_wildcards_and_trims():
    assert ts.like_pattern("50%") == "%50\\%%"
    assert ts.like_pattern("a_b") == "%a\\_b%"
    assert ts.like_pattern("c:\\x") == "%c:\\\\x%"
    assert ts.like_pattern("   ") is None and ts.like_pattern(None) is None
    assert len(ts.like_pattern("x" * 500)) == ts.MAX_QUERY_LENGTH + 2


class _F:
    """Подмена sqlalchemy.func: записывает построенное выражение."""
    def __getattr__(self, name):
        return lambda *a: f"{name}({', '.join(map(str, a))})"


def test_sql_expression_for_the_routes_and_for_psql_are_the_same_chain():
    built = ts.sql_normalized("name", _F())
    assert built.startswith("replace(") and "lower(name)" in built
    assert built.count("replace(") == len(ts.REPLACEMENTS) == ts.sql_text("name").count("replace(")


def test_routes_use_the_normalized_search_with_escape():
    from pathlib import Path
    app_dir = Path(__file__).resolve().parents[1] / "app"
    # поиск каталога и списка покупателя — общий (app/services/product_search.py), админки — свой
    for path in (app_dir / "services" / "product_search.py", app_dir / "api" / "routes_admin.py"):
        text = path.read_text(encoding="utf-8")
        assert "text_search.like_pattern(q)" in text and "escape=text_search.ESCAPE" in text, path.name
        assert ".ilike(" not in text, path.name                  # старый ILIKE без экранирования не вернулся
    for name in ("routes_catalog.py", "routes_public.py"):
        text = (app_dir / "api" / name).read_text(encoding="utf-8")
        assert "apply_search(db, query," in text and ".ilike(" not in text, name


PG_URL = os.environ.get("FULL_BACKUP_TEST_DB_URL", "")


@pytest.mark.skipif(not PG_URL or not shutil.which("psql"), reason="нужен временный PostgreSQL (FULL_BACKUP_TEST_DB_URL) и psql")
def test_search_on_real_postgresql():
    table = f"ts_{uuid.uuid4().hex[:8]}"
    names = ["ЁЛКА новогодняя", "Oʻzbekiston choy", "Сок 50% яблоко", "Сок 500 мл", "Coca-Cola Zero", "Молоко_2,5"]

    def sql(text):
        out = subprocess.run(["psql", PG_URL, "-Atq", "-v", "ON_ERROR_STOP=1", "-c", text], capture_output=True, text=True, check=True)
        return [line for line in out.stdout.splitlines() if line]

    values = ", ".join("('" + n.replace("'", "''") + "')" for n in names)
    sql(f"CREATE TABLE {table} (name text); INSERT INTO {table} VALUES {values};")
    try:
        def find(q):
            pattern = ts.like_pattern(q).replace("'", "''")
            return sorted(sql(f"SELECT name FROM {table} WHERE {ts.sql_text('name')} LIKE '{pattern}' ESCAPE '\\' ORDER BY name"))
        assert find("елка") == ["ЁЛКА новогодняя"]                 # регистр кириллицы и ё/е
        assert find("ЁЛК") == ["ЁЛКА новогодняя"]
        assert find("o'zbek") == ["Oʻzbekiston choy"]             # апостроф в запросе — обычный, в базе — ʻ
        assert find("Oʼzbekiston") == ["Oʻzbekiston choy"]
        assert find("50%") == ["Сок 50% яблоко"]                  # % — символ, а не «что угодно»
        assert find("молоко_") == ["Молоко_2,5"]
        assert find("_") == ["Молоко_2,5"]
        assert find("coca") == ["Coca-Cola Zero"]
        assert find("сок") == ["Сок 50% яблоко", "Сок 500 мл"]
    finally:
        sql(f"DROP TABLE {table}")
