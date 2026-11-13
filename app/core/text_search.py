"""
Поиск товаров по названию, бренду и SKU без учёта регистра и вариантов написания (этап 5 генерального плана; без изменения схемы).

Что считается одинаковым:
  * регистр — и латиница, и кириллица («ЁЛКА» = «ёлка»): обе стороны приводятся к нижнему регистру (в PostgreSQL lower() работает
    с кириллицей при кодировке UTF8 и локали *.UTF-8 — так в образе postgres:16; проверено на временном PostgreSQL 16);
  * «ё» и «е»;
  * узбекские апострофы: ʻ (U+02BB), ʼ (U+02BC), ‘ ’ ` ´ и обычный ' — один знак («Oʻzbekiston» = «O'zbekiston»);
  * повторные пробелы.
Знаки % и _ в запросе — обычные символы, а не шаблоны LIKE (раньше «50%» находило всё, что начинается на «50»).

Одни и те же замены применяются к запросу (normalize) и к столбцу в SQL (REPLACEMENTS -> replace(lower(столбец), …)), поэтому
поиск и база всегда согласованы. Чистые функции; построение выражения SQLAlchemy — в маршрутах (sql_normalized).
"""
from __future__ import annotations

import re
import unicodedata

APOSTROPHES = "ʻʼ‘’`´"                     # приводятся к обычному '
REPLACEMENTS: tuple[tuple[str, str], ...] = (("ё", "е"),) + tuple((ch, "'") for ch in APOSTROPHES)
ESCAPE = "\\"
MAX_QUERY_LENGTH = 100


def normalize(text: str | None) -> str:
    value = unicodedata.normalize("NFC", text or "").lower()
    for src, dst in REPLACEMENTS:
        value = value.replace(src, dst)
    return re.sub(r"\s+", " ", value).strip()


def escape_like(value: str) -> str:
    return value.replace(ESCAPE, ESCAPE * 2).replace("%", ESCAPE + "%").replace("_", ESCAPE + "_")


def like_pattern(query: str | None) -> str | None:
    """Шаблон «содержит» для LIKE … ESCAPE '\\' по нормализованному столбцу. Пустой запрос — None (без фильтра)."""
    value = normalize((query or "")[:MAX_QUERY_LENGTH])
    if not value:
        return None
    return f"%{escape_like(value)}%"


def sql_text(column: str) -> str:
    """То же выражение текстом SQL — для проверки на настоящем PostgreSQL без SQLAlchemy (tests/test_text_search.py)."""
    expr = f"lower({column})"
    for src, dst in REPLACEMENTS:
        expr = f"replace({expr}, '{src}', '{dst.replace(chr(39), chr(39) * 2)}')"
    return expr


def sql_normalized(column, func):
    """Выражение SQLAlchemy: replace(…replace(lower(column), 'ё', 'е')…). func — sqlalchemy.func (передаётся снаружи, чтобы модуль
    не зависел от SQLAlchemy)."""
    expr = func.lower(column)
    for src, dst in REPLACEMENTS:
        expr = func.replace(expr, src, dst)
    return expr
