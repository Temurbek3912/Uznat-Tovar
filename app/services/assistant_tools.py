"""
Инструменты ИИ-помощника (документ 16, §4, §11). Этап A1 — чтение каталога (kind="read"). Этап A2 — «предложить изменение»
(kind="propose": только проверка и карточка для владельца, база НЕ меняется — app/services/assistant_actions.py) и настройки
страницы (kind="ui": язык и тема этого браузера, выполняются сразу).

Правила (документ 16, §2, §6):
- модель не получает «дамп базы» и не пишет SQL: только эти инструменты, каждый отдаёт короткий итог (ограничено число строк);
- каждый инструмент требует права пользователя (Permission) — список для модели собирается по правам ЭТОГО пользователя, и сервер
  ещё раз проверяет право перед вызовом (модель могла назвать чужой инструмент);
- аргументы проверяются здесь, а не моделью: неизвестные поля отбрасываются, числа ограничиваются, строки обрезаются;
- в ответах нет паролей, токенов, ключей, журналов; о сотрудниках и копиях — только посмотреть (кто есть, когда была копия), §10.15.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func

from app.core import text_search
from app.core.security import CurrentUser, Permission, utcnow
from app.db.models import PRODUCT_ACTIVE, PRODUCT_ARCHIVED, Product, ProductBarcodeORM, ScanEventORM
from app.services import assistant_actions as actions
from app.services import category_words, price_list, scan_log
from app.services.barcode import search_keys
from app.services.catalog_upload import DEFAULT_BRAND, DEFAULT_CATEGORY
from app.services.product_images import PhotoRegistry
from app.services.product_search import apply_search

MAX_ROWS = 20                   # больше строк модель всё равно не прочитает внимательно, а память разговора маленькая
MAX_TEXT = 120                  # длинное описание и состав — обрезаются
MISSING_FIELDS = ("photo", "barcode", "price", "brand", "category")
MISSING_LABELS = {"photo": "без фото", "barcode": "без штрихкода", "price": "без цены", "brand": "без бренда",
                  "category": "без категории"}


class ToolError(ValueError):
    """Неверные аргументы инструмента: текст уходит модели, чтобы она исправилась или спросила пользователя."""


@dataclass(frozen=True)
class Tool:
    name: str
    permission: Permission
    description: str            # для модели — по-русски, коротко
    args: dict                  # {имя: подсказка} — допустимые аргументы; подсказка коротко (память модели маленькая) или ""
    run: Callable
    kind: str = "read"          # read — смотреть; propose — предложить изменение владельцу; ui — язык/тема страницы
    required: tuple = ()        # обязательные аргументы — модель не сможет их пропустить (своя JSON-схема у каждого инструмента)
    choices: dict = field(default_factory=dict)     # {аргумент: [допустимые значения]}
    answer_next: bool = False   # после успешного вызова модель только отвечает: итог уже полный (иначе она «перепроверяет»)
    wants_lang: bool = False    # инструмент получает язык владельца (args[LANG_ARG]) — от сервера, не от модели
    for_model: bool = True      # False — зовёт только программа (count_products): в подсказке и схеме модели его нет
    hidden: tuple = ()          # аргументы, которые передаёт только программа: в подсказке и схеме модели их нет

    def model_args(self) -> dict:
        return {k: v for k, v in self.args.items() if k not in self.hidden}


LANG_ARG = "_lang"              # не в args инструмента: модель его не видит и подставить не может


def _cut(value, limit: int = MAX_TEXT):
    if not isinstance(value, str):
        return value
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _limit(args: dict, default: int = 10) -> int:
    raw = args.get("limit", default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_ROWS))


def _text(args: dict, name: str) -> str:
    value = args.get(name, "")
    return value.strip()[:100] if isinstance(value, str) else ""


def _active(db):
    return db.query(Product).filter(Product.status == PRODUCT_ACTIVE)


def _codes_of(db, ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if ids:
        rows = db.query(ProductBarcodeORM.barcode, ProductBarcodeORM.product_id).filter(ProductBarcodeORM.product_id.in_(ids)).all()
        for code, pid in rows:
            out.setdefault(pid, []).append(code)
    return out


def _photo_counts(db, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    return {pid: s.total for pid, s in PhotoRegistry(db).summaries(ids).items()}


def _row(p: Product, codes: dict, photos: dict) -> dict:
    return {"sku": p.sku, "name": _cut(p.name), "brand": _cut(p.brand), "category": _cut(p.category),
            "price": p.expected_price, "barcodes": sorted(codes.get(p.id, [])), "photos": photos.get(p.id, 0)}


def _no_brand(p: Product) -> bool:
    return not (p.brand or "").strip() or p.brand == DEFAULT_BRAND


def _no_category(p: Product) -> bool:
    return not (p.category or "").strip() or p.category == DEFAULT_CATEGORY


# --- инструменты -----------------------------------------------------------------------------------------------------------------

def catalog_overview(db, args: dict) -> dict:
    products = _active(db).all()
    ids = [p.id for p in products]
    photos, codes = _photo_counts(db, ids), _codes_of(db, ids)
    categories = Counter((p.category or DEFAULT_CATEGORY) for p in products)
    priced = sorted((p for p in products if p.expected_price is not None), key=lambda p: (-p.expected_price, p.sku or ""))
    return {
        "active": len(products),
        "archived": db.query(Product).filter(Product.status == PRODUCT_ARCHIVED).count(),
        "without_photo": sum(1 for p in products if not photos.get(p.id)),
        "without_barcode": sum(1 for p in products if p.id not in codes),
        "without_price": sum(1 for p in products if p.expected_price is None),
        "without_brand": sum(1 for p in products if _no_brand(p)),
        "without_category": sum(1 for p in products if _no_category(p)),
        "categories": [{"name": _cut(name), "count": n} for name, n in categories.most_common(15)],
        "categories_total": len(categories),
        # «Какой самый дорогой товар?» — живая qwen3-vl-4b (2026-11-24) без этих строк отвечала «у нас нет цен на товары».
        "most_expensive": [{"name": _cut(p.name), "price": p.expected_price} for p in priced[:3]],
        "cheapest": [{"name": _cut(p.name), "price": p.expected_price} for p in priced[::-1][:3]],
    }


def find_products(db, args: dict) -> dict:
    query = _text(args, "query")
    if not query:
        raise ToolError("Нужен аргумент query: что искать (название, бренд, артикул или штрихкод).")
    q, exact = apply_search(db, _active(db), query)
    total, by_word = q.count(), ""
    if not total:
        # Поиск ищет фразу целиком, а модель пишет её по-своему: «Coca-Cola 0,33 l» (латинская l) при товаре «0,33 л» не
        # находилось ничего. Тогда — по самому длинному слову фразы, и модель видит, по какому слову нашлось.
        for word in sorted({w for w in re.split(r"[\s,.;:()«»\"']+", query) if len(w) >= 3}, key=lambda w: (-len(w), w)):
            if word == query:
                continue
            q, exact = apply_search(db, _active(db), word)
            total = q.count()
            if total:
                by_word = word
                break
    stemmed: list[Product] = []
    if not total:
        # «Какая цена у Фанты?»: модель ищет «Фанты» — кириллицей и в падеже, а товар — «Fanta 0,5 л». Тогда — слова названия
        # в любом алфавите и по основе слова (price_list.by_stem, §10.13): сначала фраза, потом её слова по одному.
        active = _active(db).order_by(Product.sku).all()
        for part in [query, *sorted({w for w in re.split(r"[\s,.;:()«»\"'?!]+", query) if len(w) >= 3} - {query},
                                     key=lambda w: (-len(w), w))]:
            stemmed = price_list.by_stem(part, active)
            if stemmed:
                total, by_word = len(stemmed), part
                break
    order = (exact.desc(), Product.sku) if exact is not None else (Product.sku,)
    products = stemmed[:_limit(args)] if stemmed else q.order_by(*order).limit(_limit(args)).all()
    ids = [p.id for p in products]
    codes, photos = _codes_of(db, ids), _photo_counts(db, ids)
    out = {"query": query, "total": total, "items": [_row(p, codes, photos) for p in products]}
    if by_word:
        out["note"] = f"Точного совпадения нет; найдено по слову «{by_word}» — проверь, тот ли это товар."
    named = _category_named(db, query) if total else []
    if named:
        # «Сколько у нас видов сладостей?»: модель искала «сладости» и отвечала «2» — слово есть в названиях двух товаров, а в
        # категории «Сладости» их 107 (2026-11-28). Названа категория — ответ о ней: её товары и её число; товары, где слово
        # есть в названии, — отдельно (by_name).
        in_cat = _active(db).filter(Product.category.in_(named)).order_by(Product.sku)
        cat_total = in_cat.count()
        rows = in_cat.limit(_limit(args)).all()
        ids = [p.id for p in rows]
        codes, photos = _codes_of(db, ids), _photo_counts(db, ids)
        top = in_cat.filter(Product.expected_price.isnot(None)).order_by(Product.expected_price.desc(), Product.sku).first()
        out = {"query": query, "category": ", ".join(named), "category_total": cat_total, "total": cat_total,
               "items": [_row(p, codes, photos) for p in rows],
               "category_items": [{"name": _cut(p.name), "price": p.expected_price} for p in rows[:5]],
               **({"most_expensive": {"name": _cut(top.name), "price": top.expected_price}} if top else {}),
               "by_name_total": total, "by_name": [_cut(p.name, 60) for p in products[:5]],
               "summary": (f"«{_cut(query, 40)}» — это категория {', '.join(named)}: в ней {cat_total} товаров. Отдельно: "
                           f"слово «{_cut(query, 40)}» есть в названии {total} товаров (by_name).")}
    if not total:
        # «Gazaklar narxini 10% oshiring»: модель искала категорию как товар и отвечала «не найдено» (2026-11-17). Слово —
        # категория по словарю языков: говорим модели, какая это категория в каталоге.
        cats = category_words.matching_categories(query, sorted({c for (c,) in _active(db).with_entities(Product.category)
                                                                 .distinct().all() if c}))
        if cats:
            out["note"] = (f"Товара с таким названием нет, но «{query}» — это категория {', '.join(cats)}. Изменить все её "
                           f"товары — propose_bulk(where=category, query={cats[0]}).")
            # «Sut qancha turadi?» (молоко): ответ — товары этой категории (2026-11-24); без них модель пересказывала
            # владельцу подсказку «propose_bulk(…)».
            in_cat = _active(db).filter(Product.category.in_(cats)).order_by(Product.sku).limit(5).all()
            out["category"] = cats[0]
            out["category_items"] = [{"name": _cut(p.name), "price": p.expected_price} for p in in_cat]
    return out


def _key(text) -> str:
    """«Nestlé» = «Nestle», «Coca Cola» = «Coca-Cola»: без регистра, знаков и надстрочных знаков."""
    import unicodedata  # noqa: PLC0415
    flat = unicodedata.normalize("NFKD", str(text or "")).lower()
    return re.sub(r"[\W_]+", "", "".join(ch for ch in flat if not unicodedata.combining(ch)))


def _price_arg(args: dict, name: str) -> float | None:
    raw = args.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = float(str(raw).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError as exc:
        raise ToolError(f"Аргумент {name}: число (цена в сумах).") from exc
    return value if value >= 0 else None


# Готовая фраза итога count_products — на языке владельца: живая qwen3-vl-4b переписывает её как есть, а русскую по-узбекски
# переводила с ошибками (как scan_problems). {what} — «в категории «Сладости»», «бренда Heinz», «без цены»…
_COUNT_SUMMARY = {
    "ru": "{what}: {n}.", "uz": "{what}: {n} ta.", "uz-Cyrl": "{what}: {n} та.", "en": "{what}: {n}.",
}
_COUNT_WORDS = {
    "ru": {"all": "Товаров в продаже", "cat": "Товаров в категории {c}", "brand": "Товаров {b}",
           "photo": "без фото", "barcode": "без штрихкода", "price": "без цены", "brand_f": "без бренда",
           "category": "без категории", "from": "цена от {v} сум", "to": "цена до {v} сум",
           "no_cat": "Категории «{c}» в каталоге нет. Есть: {cats}.", "no_brand": "Товаров «{b}» в каталоге нет."},
    "uz": {"all": "Sotuvdagi mahsulotlar", "cat": "{c} kategoriyasidagi mahsulotlar", "brand": "{b} mahsulotlari",
           "photo": "rasmsiz", "barcode": "shtrixkodsiz", "price": "narxsiz", "brand_f": "brendsiz",
           "category": "kategoriyasiz", "from": "narxi {v} soʻmdan", "to": "narxi {v} soʻmgacha",
           "no_cat": "Katalogda «{c}» kategoriyasi yoʻq. Bor: {cats}.", "no_brand": "Katalogda «{b}» mahsulotlari yoʻq."},
    "uz-Cyrl": {"all": "Сотувдаги маҳсулотлар", "cat": "{c} категориясидаги маҳсулотлар", "brand": "{b} маҳсулотлари",
                "photo": "расмсиз", "barcode": "штрихкодсиз", "price": "нархсиз", "brand_f": "брендсиз",
                "category": "категориясиз", "from": "нархи {v} сўмдан", "to": "нархи {v} сўмгача",
                "no_cat": "Каталогда «{c}» категорияси йўқ. Бор: {cats}.", "no_brand": "Каталогда «{b}» маҳсулотлари йўқ."},
    "en": {"all": "Products for sale", "cat": "Products in the {c} category", "brand": "{b} products",
           "photo": "without photo", "barcode": "without barcode", "price": "without price", "brand_f": "without brand",
           "category": "without category", "from": "price from {v} UZS", "to": "price up to {v} UZS",
           "no_cat": "There is no «{c}» category. There are: {cats}.",
           "no_brand": "No «{b}» products in the catalog."},
}
COUNT_WITHOUT = MISSING_FIELDS
COUNT_ALL_TEXT = {"ru": "Товаров в каталоге: {n}, в архиве: {a}.", "uz": "Katalogda {n} ta mahsulot, arxivda {a} ta.",
                  "uz-Cyrl": "Каталогда {n} та маҳсулот, архивда {a} та.", "en": "Products in the catalog: {n}, archived: {a}."}


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def count_products(db, args: dict) -> dict:
    """Сколько товаров: категория, бренд, «без цены / фото / штрихкода / бренда / категории», цена от–до. Считает программа
    (модель только выбирает аргументы): владелец на «сколько у нас видов сладостей» получил «2» — find_products искал слово в
    названиях, а в категории «Сладости» 107 товаров (2026-11-28). Итог: total, summary (готовая фраза на языке владельца),
    5 примеров и самый дорогой товар среди найденных."""
    lang = args.get(LANG_ARG) if args.get(LANG_ARG) in _COUNT_WORDS else "ru"
    words = _COUNT_WORDS[lang]
    products = _active(db).order_by(Product.sku).all()
    cats_all = sorted({p.category for p in products if p.category and p.category != DEFAULT_CATEGORY})
    asked_cat, asked_brand = _text(args, "category"), _text(args, "brand")
    without = _text(args, "without").lower()
    if without and without not in COUNT_WITHOUT:
        raise ToolError("Аргумент without: одно из " + ", ".join(COUNT_WITHOUT) + ".")
    lo, hi = _price_arg(args, "price_from"), _price_arg(args, "price_to")
    out: dict = {}
    what = [words["all"]]
    if asked_cat:
        cats = categories_in(asked_cat, [p.category for p in products])
        if not cats:
            text = words["no_cat"].format(c=_cut(asked_cat, 40), cats=", ".join(cats_all))
            return {"total": 0, "category": [], "summary": text, "items": []}
        products = [p for p in products if p.category in cats]
        out["category"] = cats
        what = [words["cat"].format(c=", ".join(f"«{c}»" for c in cats))]
    if asked_brand:
        key = _key(asked_brand)
        # Бренд тот же или он есть в названии: «CocaCola 1» с брендом «Не указан» — тоже Coca-Cola.
        found = [p for p in products if key and (_key(p.brand) == key or key in _key(p.name))] if len(key) >= 2 else []
        if not found:
            return {"total": 0, "brand": asked_brand, "summary": words["no_brand"].format(b=_cut(asked_brand, 40)),
                    "items": []}
        products = found
        out["brand"] = asked_brand
        if not asked_cat:
            what = [words["brand"].format(b=_cut(asked_brand, 40))]
        else:
            what.append(_cut(asked_brand, 40))
    if without:
        ids = [p.id for p in products]
        if without == "photo":
            photos = _photo_counts(db, ids)
            products = [p for p in products if not photos.get(p.id)]
        elif without == "barcode":
            codes = _codes_of(db, ids)
            products = [p for p in products if p.id not in codes]
        else:
            test = {"price": lambda p: p.expected_price is None, "brand": _no_brand, "category": _no_category}[without]
            products = [p for p in products if test(p)]
        out["without"] = without
        what.append(words["brand_f" if without == "brand" else without])
    if lo is not None or hi is not None:
        products = [p for p in products if p.expected_price is not None and (lo is None or p.expected_price >= lo)
                    and (hi is None or p.expected_price <= hi)]
        if lo is not None:
            what.append(words["from"].format(v=_money(lo)))
        if hi is not None:
            what.append(words["to"].format(v=_money(hi)))
    total = len(products)
    summary = _COUNT_SUMMARY[lang].format(what=", ".join(what), n=total)
    if not (asked_cat or asked_brand or without or lo is not None or hi is not None):
        archived = db.query(Product).filter(Product.status == PRODUCT_ARCHIVED).count()
        summary = COUNT_ALL_TEXT[lang].format(n=total, a=archived)
        out["archived"] = archived
        out["categories_total"] = len({p.category or DEFAULT_CATEGORY for p in products})
    priced = [p for p in products if p.expected_price is not None]
    if priced and without != "price":
        top = max(priced, key=lambda p: (p.expected_price, p.sku or ""))
        low = min(priced, key=lambda p: (p.expected_price, p.sku or ""))
        out["most_expensive"] = {"name": _cut(top.name), "price": top.expected_price}
        out["cheapest"] = {"name": _cut(low.name), "price": low.expected_price}
    out.update({"total": total, "summary": summary, "items": [_cut(p.name, 60) for p in products[:5]]})
    return out


def _category_named(db, query: str) -> list[str]:
    """Категории, которые запрос называет как категорию, а не как товар: название категории прямо («сладости», «Бакалея»)
    или слово словаря во множественном числе («сладостей», «shirinliklar», «чипсы»). «Кефир», «молоко» — товар: по ним
    ищутся товары, как раньше (иначе «Сколько стоит кефир?» отвечал бы о всех молочных)."""
    from app.services import command_words  # noqa: PLC0415
    cats = sorted({c for (c,) in _active(db).with_entities(Product.category).distinct().all()
                   if c and c != DEFAULT_CATEGORY})
    said = category_words.latin(query)
    direct = [c for c in cats if category_words.latin(c) in said]
    if direct:
        return direct[:5]
    words = [w for w in re.findall(r"[a-z]+", said) if command_words.plural(w) and category_words.groups_of(w)]
    return category_words.matching_categories(" ".join(words), cats)[:5] if words else []


def categories_in(text: str, categories) -> list[str]:
    """Категории каталога, названные в тексте: прямо («Сладости») или словом на другом языке («shirinliklar»,
    app/services/category_words.py). «Без категории» не считается."""
    from app.services import category_words  # noqa: PLC0415
    from app.services.catalog_upload import DEFAULT_CATEGORY  # noqa: PLC0415
    cats = sorted({c for c in categories if c and c != DEFAULT_CATEGORY})
    said = category_words.latin(text)
    direct = [c for c in cats if category_words.latin(c) in said]
    return sorted(set(direct) | set(category_words.matching_categories(text, cats)))[:5]


def products_missing(db, args: dict) -> dict:
    field = _text(args, "field").lower()
    if field not in MISSING_FIELDS:
        raise ToolError("Аргумент field: одно из " + ", ".join(MISSING_FIELDS) + ".")
    products = _active(db).order_by(Product.sku).all()
    asked = _text(args, "category")
    cats: list[str] = []
    if asked and field != "category":
        # «Какие сладости без цены?» — только товары названной категории (2026-11-27: помощник называл и не сладости).
        cats = categories_in(asked, [p.category for p in products])
        products = [p for p in products if p.category in cats]
    ids = [p.id for p in products]
    photos, codes = _photo_counts(db, ids), _codes_of(db, ids)
    test = {
        "photo": lambda p: not photos.get(p.id),
        "barcode": lambda p: p.id not in codes,
        "price": lambda p: p.expected_price is None,
        "brand": _no_brand,
        "category": _no_category,
    }[field]
    found = [p for p in products if test(p)]
    limit = _limit(args)
    # Готовая фраза с числом: живая qwen3-vl-4b (2026-11-17) на «Сколько товаров без штрихкода?» брала первый товар списка,
    # смотрела его карточку и предлагала ему выдуманный штрихкод — вместо ответа числом.
    summary = f"Товаров {MISSING_LABELS[field]}: {len(found)} из {len(products)}."
    if asked and field != "category":
        summary = (f"В категории {', '.join(cats)} товаров {MISSING_LABELS[field]}: {len(found)} из {len(products)}." if cats
                   else f"Категории «{_cut(asked)}» в каталоге нет.")
    if len(found) > limit:
        summary += f" Ниже первые {limit}; весь список — на странице «Товары»."
    if field in ("category", "brand"):
        # «Убери в архив все газированные напитки» модель уводила сюда и отвечала «нет товаров без категории» (2026-11-17)
        summary += (f" Это товары БЕЗ {'категории' if field == 'category' else 'бренда'}. Если владелец просит изменить все "
                    f"товары какой-то {'категории' if field == 'category' else 'бренда'} — propose_bulk(where={field}, …).")
    return {"field": field, "total": len(found), "summary": summary, **({"category": cats} if asked else {}),
            "items": [{"sku": p.sku, "name": _cut(p.name)} for p in found[:limit]]}


def _same_names(products: list[Product]) -> list[list[Product]]:
    """Группы товаров с одинаковым названием (без учёта регистра, «ё/е», апострофов и лишних пробелов)."""
    groups: dict[str, list[Product]] = {}
    for p in products:
        key = " ".join(text_search.normalize(p.name).split())
        if key:
            groups.setdefault(key, []).append(p)
    return [g for g in groups.values() if len(g) > 1]


def similar_names(db, args: dict) -> dict:
    """Возможные дубли: одинаковое название (без учёта регистра, «ё/е», апострофов и лишних пробелов). Штрихкод дублем быть не
    может — один код ведёт только на один товар (это проверяет сама база)."""
    dupes = _same_names(_active(db).order_by(Product.sku).all())
    limit = _limit(args)
    return {"groups_total": len(dupes),
            "groups": [[{"sku": p.sku, "name": _cut(p.name), "brand": _cut(p.brand)} for p in g[:5]] for g in dupes[:limit]]}


def product_details(db, args: dict) -> dict:
    key = _text(args, "product")
    if not key:
        raise ToolError("Нужен аргумент product: артикул (sku) или штрихкод товара.")
    product = db.query(Product).filter(Product.sku == key).first()
    if product is None:
        codes = search_keys(key)
        if codes:
            row = db.query(ProductBarcodeORM.product_id).filter(ProductBarcodeORM.barcode.in_(codes)).first()
            product = db.get(Product, row[0]) if row else None
    if product is None:
        return {"found": False, "product": key}
    codes, photos = _codes_of(db, [product.id]), _photo_counts(db, [product.id])
    out = _row(product, codes, photos)
    out.update({"found": True, "status": product.status, "in_stock": product.in_stock,
                **{name: _cut(getattr(product, name, None)) for name in
                   ("manufacturer", "country", "net_content", "ingredients", "description")}})
    return out


FEW_PHOTOS = 2                  # 1–2 фото: по виду узнаётся хуже, нужно 2–3 с разных сторон
OFTEN = 3                       # «часто сканируют» — от стольких сканов за выбранные дни
SHOW_ROWS = 8                   # строк в каждом списке: итог для модели — до 2000 знаков


_KINDS: dict[str, dict[str, str]] = {}
_I18N_FILES = {"ru": "i18n.ru.js", "uz": "i18n.uz.js", "uz-Cyrl": "i18n.uz-cyrl.js", "en": "i18n.en.js"}
# «без названия (вид)» — сразу на языке владельца: живая qwen3-vl-4b (2026-11-17) в узбекском ответе оставляла
# «без названия (Молочный продукт)» по-русски — то, что похоже на название товара, она не переводит.
_NO_NAME = {"ru": "без названия", "uz": "nomsiz", "uz-Cyrl": "номсиз", "en": "no name"}


def _page_label(key: str, lang: str = "ru") -> str:
    """Ключ словаря страниц (kind.drink, role.admin) -> подпись на языке владельца (frontend/i18n.<язык>.js). Нет подписи на
    этом языке — по-русски; нет и её — код после точки как есть («dairy» модель печатала владельцу прямо так)."""
    lang = lang if lang in _I18N_FILES else "ru"
    if lang not in _KINDS:
        try:
            text = (Path(__file__).resolve().parents[2] / "frontend" / _I18N_FILES[lang]).read_text(encoding="utf-8")
            _KINDS[lang] = dict(re.findall(r'"([\w.]+)":\s*"([^"]+)"', text))
        except OSError:
            _KINDS[lang] = {}
    label = _KINDS[lang].get(key)
    return label if label else (key.split(".", 1)[-1] if lang == "ru" else _page_label(key, "ru"))


def _kind_label(code: str, lang: str = "ru") -> str:
    return _page_label(f"kind.{code}", lang)


def _days(args: dict) -> int:
    try:
        value = int(args.get("days", 30))
    except (TypeError, ValueError):
        value = 30
    return max(1, min(value, scan_log.KEEP_DAYS))


TIPS = ("no_photo", "more_photos", "check_photos", "add_barcode")     # советы по товару: коды для страницы (i18n) и для модели
_TIP_TEXT = {"no_photo": "нет фото: по виду не узнаётся — добавьте 2–3 фото с разных сторон",
             "more_photos": "добавьте ещё 2–3 фото с разных сторон",
             "check_photos": "проверьте фото товара (на всех ли он сам) и добавьте крупное фото этикетки",
             "add_barcode": "добавьте штрихкод — по нему узнаётся сразу"}


def _tips(unsure: int, photos: int, has_code: bool) -> list[str]:
    if not photos:
        return ["no_photo"]
    tips = []
    if photos <= FEW_PHOTOS:
        tips.append("more_photos")
    elif unsure:
        tips.append("check_photos")
    if unsure and not has_code:
        tips.append("add_barcode")
    return tips


def scan_report(db, days: int) -> dict:
    """Итог сканов покупателей за days дней (app/services/scan_log.py) — числами, без готовых фраз. Его показывает страница
    помощника сразу, без модели (GET /assistant/scans, решение владельца 2026-11-17), и из него же собирается scan_problems.
      counts   — сканов по итогу: matched, uncertain, not_in_catalog, unknown;
      weak     — товары, которые узнавались неуверенно или часто сканируются при 1–2 фото (tips — коды советов, TIPS);
      missing  — что сканировали, а в каталоге нет: name (как назвала программа) или kind (вид), times;
      started  — были ли сканы вообще (False — программа ещё не начала их считать)."""
    days = max(1, min(int(days), scan_log.KEEP_DAYS))
    since = utcnow() - timedelta(days=days)
    rows = db.query(ScanEventORM.status, ScanEventORM.product_id, ScanEventORM.guess_name, ScanEventORM.guess_kind)         .filter(ScanEventORM.at >= since).all()
    counts = Counter(r.status for r in rows)
    per: dict[str, list[int]] = {}
    for r in rows:
        if r.product_id:
            item = per.setdefault(r.product_id, [0, 0])
            item[0] += 1
            item[1] += r.status == "uncertain"
    products = {p.id: p for p in _active(db).filter(Product.id.in_(list(per))).all()} if per else {}
    photos, codes = _photo_counts(db, list(products)), _codes_of(db, list(products))
    weak = []
    for pid, (scans, unsure) in per.items():
        p = products.get(pid)
        if p is None or not (unsure or (scans >= OFTEN and photos.get(pid, 0) <= FEW_PHOTOS)):
            continue
        weak.append({"sku": p.sku, "name": _cut(p.name, 60), "scans": scans, "unsure": unsure, "photos": photos.get(pid, 0),
                     "tips": _tips(unsure, photos.get(pid, 0), pid in codes)})
    weak.sort(key=lambda w: (-w["unsure"], -w["scans"], w["sku"]))
    missing: dict[tuple, dict] = {}
    for r in rows:
        if r.status != "not_in_catalog":
            continue
        name = (r.guess_name or "").strip()
        if not name and not r.guess_kind:
            continue
        key = (" ".join(text_search.normalize(name).split()), "") if name else ("", r.guess_kind)
        item = missing.setdefault(key, {"name": _cut(name, 60) or None, "kind": None if name else r.guess_kind, "times": 0})
        item["times"] += 1
    return {"days": days, "scans": len(rows), "started": db.query(func.min(ScanEventORM.at)).scalar() is not None,
            "counts": {s: counts[s] for s in scan_log.STATUSES}, "weak_total": len(weak), "weak": weak[:SHOW_ROWS],
            "missing_total": len(missing),
            "missing": sorted(missing.values(), key=lambda m: -m["times"])[:SHOW_ROWS],
            "many_unknown": bool(rows) and counts["unknown"] * 4 >= len(rows)}


HINT_DAYS = 30                  # подсказки по сканам — за 30 дней (как сводка сканов по умолчанию)
HINT_NAMES = 3                  # названий в подсказке: «Fanta, Sprite, Pepsi и ещё 37»
HINTS = ("empty", "no_photo", "no_price", "no_barcode", "same_name", "weak", "missing")


def hints(db) -> dict:
    """Подсказки по своей инициативе (документ 16, §10.11, решение владельца 2026-11-17): «у 40 товаров нет фото», «у 5 нет
    цены», «3 товара с одинаковым названием», «товар часто сканируют, а узнаёт неуверенно». Считает программа, модель не нужна
    (числа всегда верные); помощник ничего не меняет — страница показывает подсказку и кнопку с готовым вопросом.
    Каждая подсказка: code (HINTS), n — сколько (товаров, групп одинаковых названий, сканированного вне каталога),
    names — первые HINT_NAMES названий."""
    products = _active(db).order_by(Product.sku).all()
    out: list[dict] = []
    if not products:
        return {"active": 0, "hints": [{"code": "empty", "n": 0, "names": []}]}
    ids = [p.id for p in products]
    photos, codes = _photo_counts(db, ids), _codes_of(db, ids)
    tests = (("no_photo", lambda p: not photos.get(p.id)), ("no_price", lambda p: p.expected_price is None),
             ("no_barcode", lambda p: p.id not in codes))
    for code, test in tests:
        found = [p for p in products if test(p)]
        if found:
            out.append({"code": code, "n": len(found), "names": [_cut(p.name, 60) for p in found[:HINT_NAMES]]})
    dupes = _same_names(products)
    if dupes:
        out.append({"code": "same_name", "n": len(dupes), "names": [_cut(g[0].name, 60) for g in dupes[:HINT_NAMES]]})
    rep = scan_report(db, HINT_DAYS)
    if rep["weak_total"]:
        out.append({"code": "weak", "n": rep["weak_total"], "names": [w["name"] for w in rep["weak"][:HINT_NAMES]]})
    if rep["missing_total"]:
        out.append({"code": "missing", "n": rep["missing_total"],
                    "names": [m["name"] for m in rep["missing"] if m["name"]][:HINT_NAMES]})
    return {"active": len(products), "days": HINT_DAYS, "hints": out}


def scan_problems(db, args: dict) -> dict:
    """«Почему не узнал» (документ 16, §10.5): по сканам покупателей за N дней — товары, которые узнавались неуверенно или
    часто сканируются при 1–2 фото; что сканировали, а в каталоге этого нет; сколько не поняли совсем. Числа — scan_report."""
    lang = args.get(LANG_ARG) if args.get(LANG_ARG) in _NO_NAME else "ru"
    rep = scan_report(db, _days(args))
    days, counts = rep["days"], rep["counts"]
    # Счёт — сканов, а не товаров: с полями «unsure: 7» модель писала «7 товаров не распознаются». Готовая фраза summary
    # переписывается моделью без ошибок.
    # «Программа узнала» — с подлежащим: с «узнано …» модель писала «покупатели не узнают товары».
    out = {"days": days, "scans": rep["scans"], "summary": (
        f"За {days} дн. покупатели сделали {rep['scans']} сканов. Из этих сканов программа узнала товар уверенно — "
        f"{counts['matched']}, неуверенно — {counts['uncertain']}; товара не было в каталоге — {counts['not_in_catalog']}; "
        f"не поняла, что на фото, — {counts['unknown']}.")}
    if not rep["started"]:
        out["note"] = "Сканов покупателей ещё нет: программа начала их считать с этого обновления."
    out["weak_total"] = rep["weak_total"]
    out["weak"] = [{k: w[k] for k in ("sku", "name", "scans", "unsure", "photos")} | {"advice": "; ".join(_TIP_TEXT[t] for t in w["tips"])}
                   for w in rep["weak"]]
    out["missing"] = [{"name": m["name"] or f"{_NO_NAME[lang]} ({_kind_label(m['kind'], lang)})", "times": m["times"]}
                      for m in rep["missing"]]
    if out["missing"]:
        out["missing_advice"] = "этих товаров нет в каталоге (уже проверено, искать не нужно): если они продаются — добавьте их с фото"
    if rep["many_unknown"]:
        out["not_understood_advice"] = "много сканов не поняли совсем: попросите снимать ближе, при хорошем свете, штрихкодом к камере"
    return out


# --- сотрудники и резервные копии: только посмотреть (этап A4, первая часть, §10.15) ----------------------------------------------
# Права — те же, что у страницы «Управление» (USER_MANAGE — список сотрудников, STORE_MANAGE — копии). В итоге нет паролей, хешей,
# токенов, ПИН-кодов, второго фактора, почты Google, причин блокировки (их пишет человек) и путей к папкам — по построению: эти
# поля сюда просто не берутся. Менять — только на странице «Управление».

_STATES = ("active", "pending_first_login", "blocked", "expired", "archived")
_STATE_KEYS = {"active": "admin.active", "pending_first_login": "admin.awaiting_first_login", "blocked": "admin.disabled",
               "expired": "admin.status_expired", "archived": "admin.status_archived"}
_STATE_WORDS = {"active": "действуют", "pending_first_login": "ждут первого входа", "blocked": "заблокированы",
                "expired": "срок доступа истёк", "archived": "в архиве"}


def _change_on_admin(lang: str) -> str:
    # Название страницы — как в меню на языке владельца: в узбекском ответе модель оставляла «Управление» по-русски.
    return f"Изменить что-то здесь можно только на странице «{_page_label('nav.admin', lang)}»."


def _shop_tz():
    from app.core.config import get_settings
    from app.services.admin_service import tz_for
    try:
        return tz_for(get_settings().app_timezone)
    except ValueError:
        return tz_for(None)


def _when(value) -> tuple[str, int] | None:
    """Время (datetime: наивное — UTC из базы; ISO-строка с поясом — из файла копий) -> («17.11.2026 14:30», сколько дней назад)."""
    from datetime import datetime, timezone
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value) if value else None
        except ValueError:
            value = None
    if value is None:
        return None
    tz = _shop_tz()
    local = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(tz)
    return local.strftime("%d.%m.%Y %H:%M"), (utcnow().replace(tzinfo=timezone.utc).astimezone(tz).date() - local.date()).days


def _ago(days: int) -> str:
    return "сегодня" if days <= 0 else "вчера" if days == 1 else f"{days} дн. назад"


def staff_list(db, args: dict) -> dict:
    from app.services.auth_service import AuthService
    lang = args.get(LANG_ARG) if args.get(LANG_ARG) in _I18N_FILES else "ru"
    service = AuthService(db)
    rows = []
    for user in service.list_users():
        state = service.account_state(user)
        rows.append((not service.is_owner(user.id), _STATES.index(state) if state in _STATES else 9, user.username, user, state))
    rows.sort(key=lambda r: r[:3])
    counts = Counter(r[4] for r in rows)
    parts = [f"{_STATE_WORDS[s]} — {counts[s]}" for s in _STATES if counts[s]]
    summary = f"Учётных записей: {len(rows)}" + (f" ({', '.join(parts)})." if parts else ".")
    if len(rows) > MAX_ROWS:
        summary += f" Ниже первые {MAX_ROWS}."
    items = []
    for not_owner, _order, username, user, state in rows[:MAX_ROWS]:
        added = _when(user.created_at)
        item = {"login": username, "role": _page_label(f"role.{user.role}", lang),
                "state": _page_label(_STATE_KEYS.get(state, "admin.active"), lang), "added": added[0][:10] if added else ""}
        if not not_owner:
            item["owner"] = True
        items.append(item)
    return {"total": len(rows), "summary": summary + " " + _change_on_admin(lang), "items": items}


_PATHS = re.compile(r"[A-Za-z]:[\\/][^\s,;)»\"']*|(?<![\w.])/[\w.-]+(?:/[\w. -]+)+")


def _backup_source() -> dict | None:
    """Состояние копий, как его видит страница «Управление» (GET /admin/backup); None — в этой установке копий из программы нет."""
    from app.core.config import get_settings
    from app.services import lite_backup
    try:
        service = lite_backup.service_from_settings(get_settings())
    except lite_backup.BackupError:
        return None
    return service.status() if service is not None else None


def backup_status(db, args: dict) -> dict:
    lang = args.get(LANG_ARG) if args.get(LANG_ARG) in _I18N_FILES else "ru"
    status = _backup_source()
    if not status or not status.get("available"):
        return {"available": False, "summary": "В этой установке резервную копию делает не программа, а администратор сервера."}
    ok, tried = _when(status.get("last_ok")), _when(status.get("last_attempt"))
    here = sum(d.get("copies", 0) for d in status.get("destinations") or [] if d.get("kind") != "cloud")
    google = status.get("google") or {}
    out = {"available": True, "last_ok": ok[0] if ok else "", "copies_on_this_computer": here,
           "google_drive": google.get("state") or "unavailable", "running": bool(status.get("running"))}
    # Первая фраза — со страницы «Управление» на языке владельца: русскую «последняя удачная копия» живая qwen3-vl-4b
    # по-узбекски переводила как «Soniyalik nusxa» (3 из 3), готовую узбекскую фразу она переписывает как есть.
    when = (f"{ok[0]} ({_ago(ok[1])})" if lang == "ru" else ok[0]) if ok else ""
    text = [_page_label("bkp.last_ok", lang).replace("{when}", when) if ok else _page_label("bkp.never", lang)]
    if ok and ok[1] >= 3:
        text.append("Это давно: копия делается сама раз в сутки, когда программа включена.")
    error = (status.get("last_error") or "").strip()
    if error and tried and status.get("last_attempt", "") > status.get("last_ok", ""):
        text.append(f"Последняя попытка {tried[0]} не удалась: {_cut(_PATHS.sub('(папка)', error))}.")
    if out["running"]:
        text.append("Сейчас делается копия.")
    due = _when(status.get("next_due"))
    if due and not out["running"]:
        text.append(f"Следующая — около {due[0]}.")
    text.append(f"Копий на этом компьютере: {here}.")
    g_ok = _when(google.get("last_ok"))
    text.append({"connected": "Google Диск подключён. " + (_page_label("bkp.g_last_ok", lang).replace("{when}", g_ok[0])
                                                           if g_ok else "Копий там ещё нет."),
                 "lost": "Google Диск отключился — подключите его заново.",
                 "waiting": "Google Диск ждёт входа в аккаунт Google.",
                 "off": "Google Диск не подключён.",
                 "unavailable": "Google Диска в этой версии программы нет."}.get(out["google_drive"], ""))
    out["summary"] = " ".join(t for t in text if t) + " " + _change_on_admin(lang)
    return out


TOOLS: tuple[Tool, ...] = (
    Tool("catalog_overview", Permission.CATALOG_READ,
         "Сводка каталога: сколько товаров, в архиве, чего у них нет; категории.",
         {}, catalog_overview),
    Tool("find_products", Permission.CATALOG_READ,
         "Найти товары по названию, бренду, артикулу или штрихкоду.",
         {"query": "", "limit": "до 20"}, find_products, required=("query",)),
    Tool("count_products", Permission.CATALOG_READ,
         "Сколько товаров: в категории, бренда, без цены/фото/штрихкода, в диапазоне цены.",
         {"category": "", "brand": "", "without": "|".join(COUNT_WITHOUT), "price_from": "", "price_to": ""},
         count_products, choices={"without": list(COUNT_WITHOUT)}, answer_next=True, wants_lang=True,
         for_model=False),
    Tool("products_missing", Permission.CATALOG_READ,
         "Товары без фото, штрихкода, цены, бренда или категории.",
         {"field": "|".join(MISSING_FIELDS), "category": "", "limit": "до 20"}, products_missing,
         # "category" модели не показывается: с ним живая qwen3-vl-4b (2026-11-28) на «Расмсиз товарлар қайсилар?» через раз
         # звала catalog_overview (без него 6 из 6 верно, с ним 2 из 4). Категорию из слов владельца подставляет программа
         # (assistant.missing_in_category, missing_args).
         required=("field",), choices={"field": list(MISSING_FIELDS)}, answer_next=True, hidden=("category",)),
    Tool("similar_names", Permission.CATALOG_READ,
         "Дубли: товары с одинаковым названием.",
         {"limit": "до 20"}, similar_names),
    Tool("product_details", Permission.CATALOG_READ,
         "Всё о товаре по артикулу или штрихкоду.",
         {"product": "артикул или штрихкод"}, product_details, required=("product",)),
    Tool("scan_problems", Permission.CATALOG_READ,
         "Почему не узнаёт: сканы покупателей за days дней — что узнаётся плохо, чего нет в каталоге.",
         {"days": "1–90, обычно 30"}, scan_problems, answer_next=True, wants_lang=True),
    Tool("staff_list", Permission.USER_MANAGE, "Сотрудники: кто есть, роль.", {}, staff_list,
         answer_next=True, wants_lang=True),
    Tool("backup_status", Permission.STORE_MANAGE, "Резервные копии (zaxira nusxa): когда последняя.", {}, backup_status,
         answer_next=True, wants_lang=True),
)
TOOLS += (
    Tool("propose_update", Permission.CATALOG_WRITE,
         "Предложить изменить одно поле одного товара.",
         {"product": "артикул или штрихкод", "field": "|".join(actions.EDIT_FIELDS),
          "value": "новое значение; пусто — убрать; in_stock: да/нет"},
         lambda db, args, props: props.update(db, args), "propose",
         required=("product", "field", "value"), choices={"field": list(actions.EDIT_FIELDS)}),
    Tool("propose_archive", Permission.CATALOG_WRITE,
         "Предложить убрать товар в архив (вместо удаления) или вернуть из архива.",
         {"product": "артикул или штрихкод", "status": "archived|active"},
         lambda db, args, props: props.status(db, args), "propose",
         required=("product", "status"), choices={"status": ["archived", "active"]}),
    Tool("propose_add", Permission.CATALOG_WRITE,
         "Предложить новый товар; фото добавит владелец.",
         {"name": "", "price": "", "brand": "", "category": ""},
         lambda db, args, props: props.add(db, args), "propose", required=("name",)),
    Tool("propose_bulk", Permission.CATALOG_WRITE,
         "Одно изменение многим товарам (до 50).",
         {"where": "|".join(actions.BULK_WHERE), "query": "", "change": "|".join(actions.BULK_CHANGES), "value": ""},
         lambda db, args, props: props.bulk(db, args), "propose", required=("where", "query", "change", "value"),
         choices={"where": list(actions.BULK_WHERE), "change": list(actions.BULK_CHANGES)}),
    Tool("set_language", Permission.ASSISTANT_USE, "Сменить язык программы сразу.",
         {"lang": "|".join(actions.UI_LANGS)}, lambda args: actions.ui_action(args, "lang"), "ui",
         required=("lang",), choices={"lang": list(actions.UI_LANGS)}),
    Tool("set_theme", Permission.ASSISTANT_USE, "Сменить фон программы сразу.",
         {"theme": "light (светлый)|dark (тёмный)"}, lambda args: actions.ui_action(args, "theme"), "ui",
         required=("theme",), choices={"theme": list(actions.UI_THEMES)}),
)
_BY_NAME = {t.name: t for t in TOOLS}


def allowed_tools(user: CurrentUser) -> list[Tool]:
    return [t for t in TOOLS if user.has_permission(t.permission)]


def run_tool(db, user: CurrentUser, name: str, args, proposals: actions.Proposals | None = None,
             ui: list | None = None, lang: str = "ru") -> dict:
    """Вызов от имени пользователя: инструмент должен существовать И быть разрешён ему; лишние аргументы отбрасываются.
    Предложения складываются в proposals (база не меняется), действия страницы — в ui."""
    tool = _BY_NAME.get(name)
    if tool is None or not user.has_permission(tool.permission):
        raise ToolError(f"Инструмента «{_cut(str(name), 40)}» нет или он вам недоступен.")
    clean = {k: v for k, v in (args if isinstance(args, dict) else {}).items() if k in tool.args}
    if tool.wants_lang:
        clean[LANG_ARG] = lang
    try:
        if tool.kind == "propose":
            return tool.run(db, clean, proposals if proposals is not None else actions.Proposals())
        if tool.kind == "ui":
            act = tool.run(clean)
            if ui is not None:
                ui.append(act)
            return {"ok": True, "done": act}
        return tool.run(db, clean)
    except actions.ActionError as exc:
        raise ToolError(str(exc)) from exc


def get_tool(name: str) -> Tool | None:
    return _BY_NAME.get(name)
