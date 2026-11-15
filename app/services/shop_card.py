"""
Карточка товара для покупателя: какие поля видит покупатель в каждом магазине (шаг 3 владельца, 2026-11-15).

Название, бренд и категория видны всегда — по ним покупатель узнаёт товар. Остальное магазин включает в управлении
(«Админ-панель» -> «Карточка для покупателя»): цена, производитель, страна, вес/объём, состав, описание, наличие.

СРОК ГОДНОСТИ НЕ ПОКАЗЫВАЕТСЯ НИКОГДА (решение владельца 2026-11-14): он у каждой партии свой, а карточка — одна на товар;
неверный срок на экране хуже, чем никакого. Поэтому его нет ни в товаре, ни в списке полей, и включить его нельзя: код поля
срока (и любой неизвестный код) отклоняется с понятным объяснением.

Сервер отдаёт только включённые поля — скрытое нельзя увидеть даже в обход страницы. Чистый модуль: tests/test_shop_card.py.
"""
from __future__ import annotations

import json

# Порядок — как в карточке и в настройке
CARD_FIELDS: tuple[str, ...] = ("price", "net_content", "manufacturer", "country", "in_stock", "ingredients", "description")
# Набор по умолчанию (магазин ещё ничего не выбирал): короткие сведения; длинные тексты (состав, описание) — по желанию магазина
DEFAULT_FIELDS: tuple[str, ...] = ("price", "net_content", "manufacturer", "country", "in_stock")
# Коды, которыми могут попытаться включить срок годности: отказ с объяснением, а не «неизвестное поле»
EXPIRY_CODES = frozenset({"expiry", "expiry_date", "expires", "expiration", "expiration_date", "best_before", "shelf_life",
                          "use_by", "srok", "srok_godnosti"})
EXPIRY_REFUSAL = ("Срок годности покупателю не показывается никогда: он у каждой партии свой, а карточка — одна на товар. "
                  "Включить его нельзя.")
# Текстовые поля товара (столбец products.<код>), показываются как есть
TEXT_FIELDS: tuple[str, ...] = ("net_content", "manufacturer", "country", "ingredients", "description")
MAX_SHORT = 120                 # производитель, страна, вес/объём
MAX_LONG = 2000                 # состав, описание


class CardFieldsError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message           # наш текст для человека (не текст чужого исключения)
        self.code = code


def validate_fields(fields) -> tuple[str, ...]:
    """Выбор магазина -> кортеж кодов в порядке CARD_FIELDS. Срок годности или неизвестный код — CardFieldsError."""
    if not isinstance(fields, (list, tuple)):
        raise CardFieldsError("Поля карточки — список.", "card_fields_invalid")
    chosen = set()
    for item in fields:
        code = str(item or "").strip().lower()
        if code in EXPIRY_CODES or "expir" in code or "srok" in code:
            raise CardFieldsError(EXPIRY_REFUSAL, "card_field_expiry_forbidden")
        if code not in CARD_FIELDS:
            raise CardFieldsError(f"Такого поля карточки нет: {code[:40]}", "card_field_unknown")
        chosen.add(code)
    return tuple(code for code in CARD_FIELDS if code in chosen)


def parse_stored(raw: str | None) -> tuple[str, ...]:
    """Значение stores.card_fields -> кортеж кодов. Пусто — набор по умолчанию; испорченная запись или чужие коды (в том числе
    срок годности, вписанный в базу руками) — отбрасываются: показывается только то, что разрешено."""
    if raw is None or not str(raw).strip():
        return DEFAULT_FIELDS
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return DEFAULT_FIELDS
    if not isinstance(data, list):
        return DEFAULT_FIELDS
    allowed = {str(x).strip().lower() for x in data} & set(CARD_FIELDS)
    return tuple(code for code in CARD_FIELDS if code in allowed)


def dump_fields(fields: tuple[str, ...]) -> str:
    return json.dumps(list(fields))


def store_fields(store) -> tuple[str, ...]:
    return parse_stored(getattr(store, "card_fields", None) if store is not None else None)


def _price(value) -> float | None:
    return round(float(value), 2) if value is not None else None


def details_of(product, fields: tuple[str, ...]) -> dict:
    """Включённые сведения товара, только непустые. in_stock: True/False (не указано — нет в ответе)."""
    out: dict = {}
    for code in fields:
        if code in TEXT_FIELDS:
            value = (getattr(product, code, None) or "").strip()
            if value:
                out[code] = value
        elif code == "in_stock":
            value = getattr(product, "in_stock", None)
            if value is not None:
                out["in_stock"] = bool(value)
    return out


# Заглушки, которые программа ставит, когда сотрудник не указал бренд или категорию (app/services/catalog_upload.py,
# catalog_import): покупателю они ничего не говорят — в карточке пусто. Фильтр «Без категории» в списке остаётся.
PLACEHOLDER_BRANDS = frozenset({"не указан"})
PLACEHOLDER_CATEGORIES = frozenset({"без категории"})


def _shown(value, placeholders) -> str:
    text = (value or "").strip()
    return "" if text.lower() in placeholders else text


def card(product, fields: tuple[str, ...]) -> dict:
    """Публичная карточка: название, бренд, категория всегда; цена — ключ price, только если магазин её показывает
    (нет ключа — страница не пишет «цена не указана»); прочие сведения — details. Без id, штрихкодов, служебных полей."""
    out = {"name": product.name, "brand": _shown(product.brand, PLACEHOLDER_BRANDS),
           "category": _shown(product.category, PLACEHOLDER_CATEGORIES)}
    if "price" in fields:
        out["price"] = _price(product.expected_price)
    out["details"] = details_of(product, fields)
    return out


def clean_detail(code: str, value) -> str | None:
    """Ввод сотрудника для текстового поля -> значение столбца (пустая строка — убрать). Длинное — ошибка, а не обрезка."""
    text = " ".join(str(value or "").split()) if code not in ("ingredients", "description") else str(value or "").strip()
    if not text:
        return None
    limit = MAX_LONG if code in ("ingredients", "description") else MAX_SHORT
    if len(text) > limit:
        raise CardFieldsError(f"Слишком длинный текст (больше {limit} знаков).", "product_detail_too_long")
    return text
