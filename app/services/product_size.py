"""
Размер и объём товара из названия: «Pepsi 0,5 л» и «Pepsi 1,5 л» — один товар в разных размерах (решение владельца 2026-11-17,
docs/CATALOG_SOURCES.md §5).

По фото размер почти не виден: на снимке вблизи бутылка 0,5 л и 1,5 л выглядят одинаково (масштаба нет). Поэтому:
  1) штрихкод — главный признак: у каждого размера свой код, по коду ответ точный;
  2) по виду программа находит «марку и вкус», а размер берёт из надписи на этикетке (модель «зрение + текст» пишет размер
     в название, если видит его);
  3) не прочитала — ответ «похоже» со всеми размерами этого товара: покупатель выбирает сам («уточните размер»).

Здесь только разбор текста, без моделей и базы.
"""
from __future__ import annotations

import re

from app.services.product_namer import _tokens

# число + единица: «0,5 л», «1.5L», «500 мл», «330ml», «1 литр», «0,5 litr», «200 г», «1 кг», «90 g», «20 fl oz»
_NUM = r"(\d+(?:[.,]\d+)?)"
_UNITS: tuple[tuple[str, str, float], ...] = (
    (r"мл|ml|millilitr\w*|миллилитр\w*", "l", 0.001),
    (r"cl|сл", "l", 0.01),
    (r"л|l|литр\w*|litr\w*|liter\w*|litre\w*|ltr", "l", 1.0),
    (r"кг|kg|килограмм\w*|kilogramm\w*", "g", 1000.0),
    (r"мг|mg", "g", 0.001),
    (r"г|g|гр|gr|грамм\w*|gramm\w*|gram\w*", "g", 1.0),
    (r"fl\.? ?oz", "l", 0.0295735),
    (r"шт|pcs|dona", "pcs", 1.0),
)
_SIZE_RE = re.compile(
    _NUM + r"\s*(" + "|".join(u for u, _, _ in _UNITS) + r")(?![^\W\d_])", re.IGNORECASE)


def find_sizes(text: str) -> list[tuple[str, float]]:
    """Все размеры в тексте -> [(единица "l" | "g" | "pcs", величина в литрах/граммах/штуках)]."""
    out: list[tuple[str, float]] = []
    for m in _SIZE_RE.finditer(text or ""):
        value = float(m.group(1).replace(",", "."))
        unit = m.group(2).lower()
        for pattern, base, factor in _UNITS:
            if re.fullmatch(pattern, unit, flags=re.IGNORECASE):
                out.append((base, round(value * factor, 4)))
                break
    return out


def size_of(text: str) -> tuple[str, float] | None:
    """Главный размер товара (первый объём/вес; «штуки» — только если другого нет)."""
    sizes = find_sizes(text)
    main = [s for s in sizes if s[0] != "pcs"]
    return (main or sizes or [None])[0]


def name_without_size(text: str) -> frozenset[str]:
    """Слова названия без размера: «Pepsi 0,5 л» и «Пепси 1,5 литра» -> {"pepsi"} (латиница и кириллица — одна запись)."""
    return frozenset(_tokens(_SIZE_RE.sub(" ", text or "")))


def same_size(a: tuple[str, float] | None, b: tuple[str, float] | None) -> bool:
    return a is not None and b is not None and a[0] == b[0] and abs(a[1] - b[1]) <= max(a[1], b[1]) * 0.02


def size_variants(first: dict, others: list[dict]) -> list[dict]:
    """Другие размеры того же товара среди кандидатов: те же слова названия (и марки), другой размер."""
    base = name_without_size(f"{first.get('name', '')} {first.get('brand', '')}")
    size = size_of(first.get("name", ""))
    if not base or size is None:
        return []
    out = []
    for c in others:
        if not c or c.get("id") == first.get("id"):
            continue
        other_size = size_of(c.get("name", ""))
        if other_size is not None and not same_size(size, other_size) \
                and name_without_size(f"{c.get('name', '')} {c.get('brand', '')}") == base:
            out.append(c)
    return out
