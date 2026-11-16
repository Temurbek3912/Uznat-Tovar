"""
Инструменты ИИ-помощника (документ 16, §4, §11). Этап A1 — чтение каталога (kind="read"). Этап A2 — «предложить изменение»
(kind="propose": только проверка и карточка для владельца, база НЕ меняется — app/services/assistant_actions.py) и настройки
страницы (kind="ui": язык и тема этого браузера, выполняются сразу).

Правила (документ 16, §2, §6):
- модель не получает «дамп базы» и не пишет SQL: только эти инструменты, каждый отдаёт короткий итог (ограничено число строк);
- каждый инструмент требует права пользователя (Permission) — список для модели собирается по правам ЭТОГО пользователя, и сервер
  ещё раз проверяет право перед вызовом (модель могла назвать чужой инструмент);
- аргументы проверяются здесь, а не моделью: неизвестные поля отбрасываются, числа ограничиваются, строки обрезаются;
- в ответах нет паролей, токенов, пользователей, журналов — только товары.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core import text_search
from app.core.security import CurrentUser, Permission
from app.db.models import PRODUCT_ACTIVE, PRODUCT_ARCHIVED, Product, ProductBarcodeORM
from app.services import assistant_actions as actions
from app.services.barcode import search_keys
from app.services.catalog_upload import DEFAULT_BRAND, DEFAULT_CATEGORY
from app.services.product_images import PhotoRegistry
from app.services.product_search import apply_search

MAX_ROWS = 20                   # больше строк модель всё равно не прочитает внимательно, а память разговора маленькая
MAX_TEXT = 120                  # длинное описание и состав — обрезаются
MISSING_FIELDS = ("photo", "barcode", "price", "brand", "category")


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
    }


def find_products(db, args: dict) -> dict:
    query = _text(args, "query")
    if not query:
        raise ToolError("Нужен аргумент query: что искать (название, бренд, артикул или штрихкод).")
    q, exact = apply_search(db, _active(db), query)
    order = (exact.desc(), Product.sku) if exact is not None else (Product.sku,)
    total = q.count()
    products = q.order_by(*order).limit(_limit(args)).all()
    ids = [p.id for p in products]
    codes, photos = _codes_of(db, ids), _photo_counts(db, ids)
    return {"query": query, "total": total, "items": [_row(p, codes, photos) for p in products]}


def products_missing(db, args: dict) -> dict:
    field = _text(args, "field").lower()
    if field not in MISSING_FIELDS:
        raise ToolError("Аргумент field: одно из " + ", ".join(MISSING_FIELDS) + ".")
    products = _active(db).order_by(Product.sku).all()
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
    return {"field": field, "total": len(found), "items": [{"sku": p.sku, "name": _cut(p.name)} for p in found[:limit]]}


def similar_names(db, args: dict) -> dict:
    """Возможные дубли: одинаковое название (без учёта регистра, «ё/е», апострофов и лишних пробелов). Штрихкод дублем быть не
    может — один код ведёт только на один товар (это проверяет сама база)."""
    groups: dict[str, list[Product]] = {}
    for p in _active(db).order_by(Product.sku).all():
        key = " ".join(text_search.normalize(p.name).split())
        if key:
            groups.setdefault(key, []).append(p)
    dupes = [g for g in groups.values() if len(g) > 1]
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


TOOLS: tuple[Tool, ...] = (
    Tool("catalog_overview", Permission.CATALOG_READ,
         "Сводка каталога: сколько товаров, в архиве, без фото, без штрихкода, без цены, без бренда, без категории; категории.",
         {}, catalog_overview),
    Tool("find_products", Permission.CATALOG_READ,
         "Найти товары по названию, бренду, артикулу или штрихкоду.",
         {"query": "", "limit": "до 20"}, find_products, required=("query",)),
    Tool("products_missing", Permission.CATALOG_READ,
         "Товары, у которых нет фото, штрихкода, цены, бренда или категории.",
         {"field": "|".join(MISSING_FIELDS), "limit": "до 20"}, products_missing,
         required=("field",), choices={"field": list(MISSING_FIELDS)}),
    Tool("similar_names", Permission.CATALOG_READ,
         "Возможные дубли: товары с одинаковым названием.",
         {"limit": "до 20"}, similar_names),
    Tool("product_details", Permission.CATALOG_READ,
         "Всё о товаре по артикулу (sku) или штрихкоду.",
         {"product": "артикул или штрихкод"}, product_details, required=("product",)),
)
TOOLS += (
    Tool("propose_update", Permission.CATALOG_WRITE,
         "Предложить изменить одно поле товара (несколько полей — несколько вызовов).",
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
         "Предложить новый товар; фото добавит владелец (штрихкод с фото привяжется сам).",
         {"name": "", "price": "", "brand": "", "category": ""},
         lambda db, args, props: props.add(db, args), "propose", required=("name",)),
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
             ui: list | None = None) -> dict:
    """Вызов от имени пользователя: инструмент должен существовать И быть разрешён ему; лишние аргументы отбрасываются.
    Предложения складываются в proposals (база не меняется), действия страницы — в ui."""
    tool = _BY_NAME.get(name)
    if tool is None or not user.has_permission(tool.permission):
        raise ToolError(f"Инструмента «{_cut(str(name), 40)}» нет или он вам недоступен.")
    clean = {k: v for k, v in (args if isinstance(args, dict) else {}).items() if k in tool.args}
    try:
        if tool.kind == "propose":
            out = tool.run(db, clean, proposals if proposals is not None else actions.Proposals())
            if out.get("proposed"):
                out["next"] = "Скажи владельцу, что предлагаешь, и что изменится только после кнопки «Сделать»."
            return out
        if tool.kind == "ui":
            act = tool.run(clean)
            if ui is not None:
                ui.append(act)
            return {"ok": True, "done": act}
        return tool.run(db, clean)
    except actions.ActionError as exc:
        raise ToolError(str(exc)) from exc
