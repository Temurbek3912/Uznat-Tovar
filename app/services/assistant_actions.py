"""
Изменения через ИИ-помощника — этап A2 (документ 16, §11; решение владельца 2026-11-16).

Модель маленькая (4B) и может ошибиться, поэтому сама она ничего не меняет. Её инструменты «предложить …» только ПРОВЕРЯЮТ
изменение теми же правилами, что страница «Товары» (clean_name, parse_price, проверка штрихкода и повтора названия), и
возвращают ПРЕДЛОЖЕНИЕ. Страница показывает его карточкой «Помощник предлагает: … [Сделать] [Отмена]», а выполняет
POST /api/v1/assistant/act только после нажатия владельца — тем же кодом, что страница «Товары» (app/api/routes_assistant.py).

Правила владельца:
- за один вопрос — изменение только ОДНОГО товара (несколько полей можно); массовые изменения — на странице «Товары»;
- «удалить» = убрать в архив (вернуть можно); насовсем товар удаляется только руками;
- пароли, сотрудники, резервные копии и обновления помощнику недоступны вовсе;
- язык и тема — настройки этого браузера: выполняются сразу, без подтверждения (безопасно, легко вернуть).
"""
from __future__ import annotations

from app.db.models import PRODUCT_ACTIVE, PRODUCT_ARCHIVED, Product, ProductBarcodeORM
from app.services import shop_card
from app.services.barcode import explain_invalid, normalize_code, search_keys
from app.services.catalog_upload import (
    DEFAULT_BRAND,
    DEFAULT_CATEGORY,
    MAX_TEXT_LEN,
    CatalogAddError,
    clean_name,
    clean_text,
    find_duplicate,
    parse_price,
)
from app.services.product_search import apply_search

# поля товара, которые помощник может предложить изменить (артикул не меняется никогда, как и на странице «Товары»)
EDIT_FIELDS = ("name", "price", "brand", "category", "in_stock", "add_barcode", "remove_barcode", *shop_card.TEXT_FIELDS)
UI_LANGS = ("ru", "uz", "uz-Cyrl", "en")
UI_THEMES = ("light", "dark")
_YES = {"да", "есть", "в наличии", "yes", "true", "1", "ha", "bor", "ҳа", "бор"}
_NO = {"нет", "нету", "нет в наличии", "no", "false", "0", "yoʻq", "yo'q", "yoq", "йўқ"}


class ActionError(ValueError):
    """Изменение не проходит проверку: текст — для модели и для человека."""


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())


def find_product(db, key: str) -> Product | None:
    """Товар по артикулу (sku), штрихкоду или — если такой один — по названию."""
    key = _text(key)[:100]
    if not key:
        return None
    product = db.query(Product).filter(Product.sku == key).first()
    if product is None:
        codes = search_keys(key)
        if codes:
            row = db.query(ProductBarcodeORM.product_id).filter(ProductBarcodeORM.barcode.in_(codes)).first()
            product = db.get(Product, row[0]) if row else None
    if product is None:             # модель часто пишет название вместо артикула: годится, только если такой товар один
        found = apply_search(db, db.query(Product), key)[0].limit(2).all()
        product = found[0] if len(found) == 1 else None
    return product


def _owner_of(db, code: str) -> Product | None:
    link = db.get(ProductBarcodeORM, code)
    return db.get(Product, link.product_id) if link else None


def _code(raw: str) -> str:
    code = normalize_code(raw)
    if code is None:
        raise ActionError(explain_invalid(raw))
    return code


def _check_name(db, name: str, product_id: str | None) -> str:
    clean = clean_name(name)
    rows = db.query(Product.id, Product.name)
    if product_id:
        rows = rows.filter(Product.id != product_id)
    duplicate = find_duplicate(clean, [(r.id, r.name) for r in rows.all()])
    if duplicate:
        raise ActionError(f"Товар с таким названием уже есть: «{duplicate[1]}».")
    return clean


def _in_stock(value) -> bool | None:
    text = _text(value).lower().replace("ʼ", "ʻ").replace("’", "'")
    if not text:
        return None
    if text in _YES:
        return True
    if text in _NO:
        return False
    raise ActionError("Наличие: «да» (есть в продаже), «нет» или пусто (не указано).")


def current_value(db, product: Product, field: str):
    if field == "price":
        return product.expected_price
    if field in ("add_barcode", "remove_barcode"):
        return None
    return getattr(product, field, None)


def clean_value(db, product: Product, field: str, value):
    """Новое значение поля -> как его сохранит страница «Товары». Ошибка — ActionError с понятным текстом."""
    try:
        if field == "name":
            return _check_name(db, _text(value), product.id)
        if field == "price":
            return parse_price(_text(value))
        if field == "brand":
            return clean_text(value, label="бренд", max_len=MAX_TEXT_LEN, default=DEFAULT_BRAND)
        if field == "category":
            return clean_text(value, label="категорию", max_len=MAX_TEXT_LEN, default=DEFAULT_CATEGORY)
        if field in shop_card.TEXT_FIELDS:
            return shop_card.clean_detail(field, value)
        if field == "in_stock":
            return _in_stock(value)
        if field == "add_barcode":
            code = _code(_text(value))
            owner = _owner_of(db, code)
            if owner is not None and owner.id != product.id:
                raise ActionError(f"Штрихкод {code} уже у товара «{owner.name}» (артикул {owner.sku}).")
            if owner is not None:
                raise ActionError(f"Штрихкод {code} уже привязан к этому товару.")
            return code
        if field == "remove_barcode":
            code = _code(_text(value))
            owner = _owner_of(db, code)
            if owner is None or owner.id != product.id:
                raise ActionError(f"У этого товара нет штрихкода {code}.")
            return code
    except CatalogAddError as exc:
        raise ActionError(str(exc)) from exc
    except shop_card.CardFieldsError as exc:
        raise ActionError(exc.message) from exc
    raise ActionError("Поле field: одно из " + ", ".join(EDIT_FIELDS) + ".")


def _head(product: Product) -> dict:
    return {"product_id": product.id, "sku": product.sku, "name": product.name, "version": product.version or 1}


class Proposals:
    """Предложения одного вопроса. Правило владельца: один товар за раз (несколько полей — можно, сливаются в одну карточку)."""

    def __init__(self):
        self.items: list[dict] = []

    def _same_product_only(self, product: Product) -> None:
        if self.items and self.items[0].get("product_id") != product.id:
            raise ActionError("За один раз помощник меняет только один товар. Много товаров меняют на странице «Товары» "
                              "(или по одному: сначала этот, потом следующий).")

    def update(self, db, args: dict) -> dict:
        product = find_product(db, args.get("product", ""))
        if product is None:
            raise ActionError("Товар не найден: нужен точный артикул (sku) или штрихкод — сначала найдите его через find_products.")
        # Одно поле за вызов: field и value в схеме ответа модели обязательны (app/services/assistant.py, answer_schema).
        field = _text(args.get("field")).lower()
        if field not in EDIT_FIELDS:
            raise ActionError("field — что менять: " + ", ".join(EDIT_FIELDS) + ".")
        wanted = {field: args.get("value", "")}
        self._same_product_only(product)
        card = self.items[0] if self.items else None
        if card is not None and card.get("kind") != "update":
            raise ActionError("Для этого товара уже есть предложение. Сначала пусть владелец нажмёт «Сделать» или «Отмена».")
        found = {}
        for name, value in wanted.items():                # сначала проверить всё, потом складывать в карточку
            new = clean_value(db, product, name, "" if _text(value) == "-" else value)
            old = current_value(db, product, name)
            if name in ("add_barcode", "remove_barcode") or new != old:
                found[name] = (new, old)
        if not found:
            return {"ok": True, "note": "У товара уже такие значения — менять нечего."}
        if card is None:
            card = {"kind": "update", **_head(product), "changes": {}, "before": {}}
            self.items.append(card)
        for name, (new, old) in found.items():
            card["changes"][name] = new
            card["before"][name] = old
        return {"ok": True, "proposed": {name: new for name, (new, _old) in found.items()}, "product": product.sku}

    def status(self, db, args: dict) -> dict:
        product = find_product(db, args.get("product", ""))
        if product is None:
            raise ActionError("Товар не найден: нужен точный артикул (sku) или штрихкод.")
        status = _text(args.get("status")).lower() or PRODUCT_ARCHIVED
        if status not in (PRODUCT_ARCHIVED, PRODUCT_ACTIVE):
            raise ActionError("status: archived (убрать в архив) или active (вернуть из архива).")
        self._same_product_only(product)
        if self.items:
            raise ActionError("Для этого товара уже есть предложение. Сначала пусть владелец нажмёт «Сделать» или «Отмена».")
        if (product.status or PRODUCT_ACTIVE) == status:
            return {"ok": True, "note": "Товар уже " + ("в архиве." if status == PRODUCT_ARCHIVED else "в каталоге.")}
        self.items.append({"kind": "status", **_head(product), "status": status})
        return {"ok": True, "proposed": {"status": status}, "product": product.sku}

    def add(self, db, args: dict) -> dict:
        if self.items:
            raise ActionError("За один раз — одно изменение. Сначала пусть владелец нажмёт «Сделать» или «Отмена».")
        try:
            card = {
                "kind": "add",
                "name": _check_name(db, _text(args.get("name")), None),
                "price": parse_price(_text(args.get("price"))),
                "brand": clean_text(args.get("brand"), label="бренд", max_len=MAX_TEXT_LEN, default=DEFAULT_BRAND),
                "category": clean_text(args.get("category"), label="категорию", max_len=MAX_TEXT_LEN, default=DEFAULT_CATEGORY),
                "barcode": None,
            }
            if _text(args.get("barcode")):
                code = _code(_text(args.get("barcode")))
                owner = _owner_of(db, code)
                if owner is not None:
                    raise ActionError(f"Штрихкод {code} уже у товара «{owner.name}» (артикул {owner.sku}).")
                card["barcode"] = code
        except CatalogAddError as exc:
            raise ActionError(str(exc)) from exc
        self.items.append(card)
        return {"ok": True, "proposed": {k: v for k, v in card.items() if k != "kind"},
                "note": "Владелец добавит фото товара в карточке и нажмёт «Сделать»."}


def ui_action(args: dict, kind: str) -> dict:
    """Язык или тема — действие для страницы (выполняется в браузере сразу)."""
    if kind == "lang":
        lang = _text(args.get("lang"))
        if lang not in UI_LANGS:
            raise ActionError("lang: ru, uz (латиница), uz-Cyrl (кириллица) или en.")
        return {"kind": "lang", "value": lang}
    theme = _text(args.get("theme")).lower()
    if theme not in UI_THEMES:
        raise ActionError("theme: light (светлый фон) или dark (тёмный фон).")
    return {"kind": "theme", "value": theme}


def to_product_update(action: dict) -> dict:
    """Предложение «изменить товар» -> поля ProductUpdate (как их шлёт страница «Товары»)."""
    out: dict = {}
    for field, value in (action.get("changes") or {}).items():
        if field not in EDIT_FIELDS:
            raise ActionError("Неизвестное поле: " + str(field)[:40])
        if field == "price":
            if value is None:
                out["clear_price"] = True
            else:
                out["expected_price"] = value
        elif field == "in_stock":
            if value is None:
                out["clear_in_stock"] = True
            else:
                out["in_stock"] = bool(value)
        else:                                   # пустая строка — убрать (как на странице «Товары»)
            out[field] = "" if value is None else str(value)
    if not out:
        raise ActionError("Нечего менять.")
    return out


def undo_of_update(action: dict, new_version: int, new_name: str) -> dict:
    """Обратное изменение: прежние значения; добавленный штрихкод — отвязать, отвязанный — вернуть."""
    changes, before = {}, {}
    for field, value in (action.get("changes") or {}).items():
        if field == "add_barcode":
            changes["remove_barcode"], before["remove_barcode"] = value, None
        elif field == "remove_barcode":
            changes["add_barcode"], before["add_barcode"] = value, None
        else:
            changes[field], before[field] = (action.get("before") or {}).get(field), value
    return {"kind": "update", "product_id": action.get("product_id"), "sku": action.get("sku"), "name": new_name,
            "version": new_version, "changes": changes, "before": before}


def audit_fields(values: dict) -> dict:
    """Для журнала: ключи с «code» журнал отбрасывает как секрет, поэтому штрихкоды пишутся под своими именами."""
    names = {"add_barcode": "ean_added", "remove_barcode": "ean_removed"}
    return {names.get(k, k): v for k, v in (values or {}).items()}
