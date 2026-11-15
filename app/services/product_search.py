"""
Поиск товаров по названию, бренду, артикулу и штрихкоду — общий для страницы «Товары» (GET /api/v1/catalog/products) и списка
товаров покупателя (GET /api/v1/public/products). Правила сравнения текста — app/core/text_search.py; штрихкоды — полный код
в любом написании (barcode.search_keys) и часть кода от 4 цифр.
"""
from __future__ import annotations

from sqlalchemy import func, or_

from app.core import text_search
from app.db.models import Product, ProductBarcodeORM
from app.services.barcode import partial_digits, search_keys


def apply_search(db, query, q: str, *, columns=None):
    """query (по Product) + условие поиска q. Возвращает (query, exact): exact — выражение «точное совпадение штрихкода»
    для сортировки (такие товары — первыми) или None."""
    pattern = text_search.like_pattern(q)
    exact_codes = search_keys(q)
    if not pattern:
        return query, None
    cols = columns if columns is not None else (Product.sku, Product.name, Product.brand)
    conditions = [text_search.sql_normalized(col, func).like(pattern, escape=text_search.ESCAPE) for col in cols]
    by_code = db.query(ProductBarcodeORM.product_id)
    digits = partial_digits(q)
    if exact_codes:
        conditions.append(Product.id.in_(by_code.filter(ProductBarcodeORM.barcode.in_(exact_codes))))
    if digits:
        conditions.append(Product.id.in_(by_code.filter(
            ProductBarcodeORM.barcode.like(f"%{text_search.escape_like(digits)}%", escape=text_search.ESCAPE))))
    query = query.filter(or_(*conditions))
    exact = None
    if exact_codes:
        exact = Product.id.in_(db.query(ProductBarcodeORM.product_id).filter(ProductBarcodeORM.barcode.in_(exact_codes)))
    return query, exact
