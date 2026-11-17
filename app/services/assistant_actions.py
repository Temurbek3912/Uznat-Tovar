"""
Изменения через ИИ-помощника — этап A2 (документ 16, §11; решение владельца 2026-11-16).

Модель маленькая (4B) и может ошибиться, поэтому сама она ничего не меняет. Её инструменты «предложить …» только ПРОВЕРЯЮТ
изменение теми же правилами, что страница «Товары» (clean_name, parse_price, проверка штрихкода и повтора названия), и
возвращают ПРЕДЛОЖЕНИЕ. Страница показывает его карточкой «Помощник предлагает: … [Сделать] [Отмена]», а выполняет
POST /api/v1/assistant/act только после нажатия владельца — тем же кодом, что страница «Товары» (app/api/routes_assistant.py).

Правила владельца:
- за один вопрос — изменение только ОДНОГО товара (несколько полей можно) или одно МАССОВОЕ изменение (этап A3, ниже);
- «удалить» = убрать в архив (вернуть можно); насовсем товар удаляется только руками;
- пароли, сотрудники, резервные копии и обновления помощнику недоступны вовсе;
- язык и тема — настройки этого браузера: выполняются сразу, без подтверждения (безопасно, легко вернуть).
"""
from __future__ import annotations

from app.core import text_search
from app.db.models import PRODUCT_ACTIVE, PRODUCT_ARCHIVED, Product, ProductBarcodeORM
from app.services import shop_card
from app.services.barcode import explain_invalid, normalize_code, search_keys
from app.services.catalog_upload import (
    DEFAULT_BRAND,
    DEFAULT_CATEGORY,
    MAX_TEXT_LEN,
    _TRANSLIT,
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
    named = [(r.id, r.name) for r in rows.all()]
    duplicate = find_duplicate(clean, named)
    if duplicate is None:
        # Модель пишет латиницей то, что в каталоге кириллицей: «Coca-Cola 0,33 l» при товаре «Coca-Cola 0,33 л» (2026-11-17
        # на узбекском она так предложила добавить дубль вместо новой цены). Похожие буквы сравниваются как одна.
        key = _lookalike(clean)
        duplicate = next((row for row in named if _lookalike(row[1] or "") == key), None)
    if duplicate:
        raise ActionError(f"Товар с таким названием уже есть: «{duplicate[1]}». Чтобы его изменить — propose_update.")
    return clean


_LOOKALIKE = str.maketrans("авеклмнорстухгё", "abeklmhopctyxge")


def _lookalike(name: str) -> str:
    return " ".join(name.lower().replace("ʻ", "'").translate(_LOOKALIKE).split())


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
            raise ActionError("За один раз помощник меняет только один товар. Одно и то же изменение многим товарам сразу "
                              "(категория, бренд, слово в названии) — propose_bulk.")

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

    def bulk(self, db, args: dict) -> dict:
        if self.items:
            raise ActionError("За один раз — одно изменение. Сначала пусть владелец нажмёт «Сделать» или «Отмена».")
        card = bulk_card(db, args)
        self.items.append(card)
        return {"ok": True, "proposed": {"products": len(card["rows"]), "change": card["change"]}}


# --- этап A3: одно изменение многим товарам (решение владельца 2026-11-17) ------------------------------------------------------
#
# Модель маленькая, поэтому она называет только «какие товары» (where + query) и «что сделать» (change + value). Список товаров,
# новые цены и «было → станет» считает программа; карточка показывает каждую строку; «Сделать» применяет всё одной транзакцией
# (всё или ничего, app/api/routes_assistant.py), «Отменить всё» возвращает прежние значения. Название и штрихкод массово не
# меняются (у каждого товара свои).

BULK_MAX = 50                   # товаров за одно изменение; больше — уточнить отбор или страница «Товары»
BULK_WHERE = ("category", "brand", "name")
BULK_CHANGES = ("price_percent", "price_add", "price_set", "category", "brand", "in_stock", "archive", "restore")
BULK_FIELDS = ("price", "category", "brand", "in_stock", "status")      # что может стоять в строках карточки
_BULK_FIELD = {"price_percent": "price", "price_add": "price", "price_set": "price", "category": "category",
               "brand": "brand", "in_stock": "in_stock", "archive": "status", "restore": "status"}
MAX_PERCENT = 300               # +300 % — уже явная ошибка (лишний ноль); снизить можно до −90 %


def _number(value, what: str) -> float:
    text = _text(value).replace(" ", "").replace(" ", "").replace(",", ".").rstrip("%")
    try:
        return float(text)
    except ValueError:
        raise ActionError(f"{what} — это число, например 10.") from None


def _norm(text: str) -> str:
    return " ".join(text_search.normalize(text or "").split())


def _latin(text: str) -> str:
    """«Газированные напитки» -> «gazirovannye napitki» (как артикул из названия); kh и x — одна буква."""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in _norm(text).replace("ʻ", "").replace("'", ""))
    return out.replace("kh", "h").replace("x", "h")


def bulk_select(db, where: str, query: str, status: str) -> list[Product]:
    """Товары по отбору: категория или бренд содержит query (без учёта регистра и «ё/е»); name — поиск по
    названию, как на странице «Товары». Только товары с этим status (в каталоге; для «вернуть из архива» — в архиве)."""
    if where not in BULK_WHERE:
        raise ActionError("where — по чему выбрать товары: category (категория), brand (бренд) или name (слово в названии).")
    query = _text(query)[:100]
    if not query:
        raise ActionError("query — какая категория, какой бренд или какое слово в названии.")
    base = db.query(Product).filter(Product.status == status)
    if where == "name":
        return apply_search(db, base, query, columns=(Product.name,))[0].order_by(Product.sku).all()
    # «содержит», а не «равно»: «напитки» — это и «Напитки», и «Газированные напитки» (2026-11-17, живая модель на копии базы
    # магазина). Лишнее владелец увидит в карточке до «Сделать».
    # И без учёта алфавита: по-узбекски модель пишет категорию латиницей — «Sladosti» при категории «Сладости».
    key, lat = _norm(query), _latin(query)
    return [p for p in base.order_by(Product.sku).all()
            if key and (key in _norm(getattr(p, where)) or (lat and lat in _latin(getattr(p, where))))]


def _bulk_after(change: str, value, product: Product):
    """Новое значение для одного товара или None — этому товару менять нечего (нет цены для процента)."""
    if change in ("archive", "restore"):
        return PRODUCT_ARCHIVED if change == "archive" else PRODUCT_ACTIVE
    if change == "price_set":
        return value
    if change in ("price_percent", "price_add"):
        old = product.expected_price
        if old is None:
            return None
        new = old * (1 + value / 100) if change == "price_percent" else old + value
        new = float(round(new)) if change == "price_percent" else round(new, 2)    # процент — до целого сума
        if new <= 0:
            raise ActionError(f"У товара «{product.name}» цена станет {new:g} — меньше нуля. Проверьте число.")
        return parse_price(new)
    return value


def _nothing_found(db, where: str, change: str) -> str:
    """Отбор пуст: модель пишет категорию по-своему («shirinliklar» при категории «Сладости») — даём ей настоящие названия,
    чтобы она сразу повторила вызов с ними."""
    text = "По этому отбору товаров нет."
    if where in ("category", "brand"):
        status = PRODUCT_ARCHIVED if change == "restore" else PRODUCT_ACTIVE
        column = Product.category if where == "category" else Product.brand
        names = sorted({n for (n,) in db.query(column).filter(Product.status == status).distinct().all() if n})[:20]
        if names:
            text += (" Категории" if where == "category" else " Бренды") + " в каталоге: " + ", ".join(names) + \
                ". Повтори propose_bulk с одним из этих названий, как оно написано."
    return text


def bulk_card(db, args: dict) -> dict:
    where = _text(args.get("where")).lower()
    change = _text(args.get("change")).lower()
    if change not in BULK_CHANGES:
        raise ActionError("change — что сделать: " + ", ".join(BULK_CHANGES) + ".")
    field = _BULK_FIELD[change]
    raw = args.get("value")
    try:
        if change == "price_percent":
            value = _number(raw, "Процент")
            if not -90 <= value <= MAX_PERCENT or value == 0:
                raise ActionError(f"Процент — от −90 до +{MAX_PERCENT}, не ноль.")
        elif change == "price_add":
            value = _number(raw, "Сколько прибавить к цене")
            if value == 0:
                raise ActionError("Прибавить 0 — ничего не изменится.")
        elif change == "price_set":
            value = parse_price(_text(raw))
            if value is None:
                raise ActionError("Новая цена — число, например 9900.")
        elif change == "brand":
            value = clean_text(raw, label="бренд", max_len=MAX_TEXT_LEN, default=DEFAULT_BRAND)
        elif change == "category":
            value = clean_text(raw, label="категорию", max_len=MAX_TEXT_LEN, default=DEFAULT_CATEGORY)
        elif change == "in_stock":
            value = _in_stock(raw)
        else:
            value = None
    except CatalogAddError as exc:
        raise ActionError(str(exc)) from exc
    products = bulk_select(db, where, args.get("query"), PRODUCT_ARCHIVED if change == "restore" else PRODUCT_ACTIVE)
    if not products:
        raise ActionError(_nothing_found(db, where, change))
    rows, skipped = [], 0
    for p in products:
        before = (p.status or PRODUCT_ACTIVE) if field == "status" else current_value(db, p, field)
        try:
            after = _bulk_after(change, value, p)
        except CatalogAddError as exc:              # например, цена стала слишком большой
            raise ActionError(f"«{p.name}»: {exc}") from exc
        if (after is None and change in ("price_percent", "price_add")) or after == before:
            skipped += 1                             # без цены или уже такое значение — не трогаем
            continue
        rows.append({**_head(p), "before": before, "after": after})
    if not rows:
        raise ActionError(f"У всех {len(products)} товаров по этому отбору менять нечего (уже так или нет цены).")
    if len(rows) > BULK_MAX:
        raise ActionError(f"Подходит {len(rows)} товаров — за раз не больше {BULK_MAX}. Уточните отбор или измените их на "
                          "странице «Товары».")
    return {"kind": "bulk", "field": field, "change": change, "value": value, "where": where,
            "query": _text(args.get("query"))[:100], "rows": rows, "skipped": skipped}


def clean_bulk_value(field: str, value):
    """Значение строки массового изменения перед записью: сервер не верит странице и проверяет его заново."""
    try:
        if field == "price":
            return parse_price(value) if value not in (None, "") else None
        if field == "brand":
            return clean_text(value, label="бренд", max_len=MAX_TEXT_LEN, default=DEFAULT_BRAND)
        if field == "category":
            return clean_text(value, label="категорию", max_len=MAX_TEXT_LEN, default=DEFAULT_CATEGORY)
        if field == "in_stock":
            return value if value in (True, False, None) else _in_stock(value)
        if field == "status" and value in (PRODUCT_ARCHIVED, PRODUCT_ACTIVE):
            return value
    except CatalogAddError as exc:
        raise ActionError(str(exc)) from exc
    raise ActionError("Помощник предложил непонятное изменение — попросите ещё раз.")


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
