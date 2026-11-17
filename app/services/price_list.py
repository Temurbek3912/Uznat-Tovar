"""
Список «товар — цена» текстом: помощь с импортом, этап A3 (решение владельца 2026-11-17, документ 16, §10.9).

Владелец пишет помощнику столбиком:

    Новые цены:
    Fanta 0,5 — 9500
    Coca-Cola 0,33 л: 8 000 сум
    Pringles 33000
    Sprite 0,5 — 9000

Список разбирает ПРОГРАММА, а не модель: модель маленькая (4B, 4096 токенов на всё) и в длинном списке пропустила бы строку или
перепутала цену. По каждой строке программа ищет товар в каталоге и готовит одну карточку массового изменения цены (как у
propose_bulk): найденные товары — «было → станет», «Сделать» — всё или ничего одной транзакцией, «Отменить всё»
(app/api/routes_assistant.py, _act_bulk). Чего нет в каталоге — «новый товар»: он добавляется с фото (иначе его не узнать по
снимку), поэтому сам список товары не создаёт: у такой строки на странице кнопка «Добавить … с фото» открывает обычную карточку
нового товара с названием и ценой из строки (POST /assistant/act/add, без фото — отказ). Ошибки — по строкам: цена не понята, подходит несколько товаров, товар в
архиве, товар повторяется.

Список ли это. Не меньше двух строк «название + цена» (первая строка без цифр — заголовок, она пропускается), строк с ценой больше,
чем без неё, и нет слов «поднять / снизить», «%» и «категория»: «Подними цену Fanta на 1000\\nи Pringles на 2000» — это не
список новых цен, такой вопрос, как раньше, разбирает модель (app/services/price_words.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.db.models import PRODUCT_ACTIVE, PRODUCT_ARCHIVED, Product, ProductBarcodeORM
from app.services import category_words, price_words
from app.services.barcode import search_keys
from app.services.catalog_upload import CatalogAddError, parse_price

MAX_LINES = 50                  # строк за раз — как товаров в массовом изменении (assistant_actions.BULK_MAX)
MAX_LINE_CHARS = 120
MAX_NAMES = 3                   # сколько похожих товаров назвать в строке «подходит несколько»

_SPACES = "   "
_MONEY = r"(?:(?:сум|сўм|so[ʻ'’`]?m|som|sum|uzs|руб\w*|р)\.?)"
_THOUSANDS = r"(?:тыс\w*|ming|минг|k|к)\.?"
_NUMBER = r"(?:\d{1,3}(?:[ .]\d{3})+|\d+(?:[.,]\d{1,2})?)"
# цена в конце строки: «9500», «9 500», «9.500», «9500 сум», «9,5 тыс.», «12 ming soʻm»
_PRICE_END = re.compile(rf"(?<![\d.,])(?P<num>{_NUMBER})\s*(?P<k>{_THOUSANDS})?\s*{_MONEY}?\s*[.;,]?\s*$", re.I)
_PRICE_ONLY = re.compile(rf"^\s*(?P<num>{_NUMBER})\s*(?P<k>{_THOUSANDS})?\s*{_MONEY}?\s*[.;,]?\s*$", re.I)
# «Fanta — 9500», «Fanta - 9500», «Fanta: 9500», «Fanta = 9500», табуляция (из Excel)
_SEPARATOR = re.compile(r"\s+[—–-]+\s+|\s*[:=\t]\s*|\s+[—–]\s*|\s*[—–]\s+")
_BULLET = re.compile(r"^\s*(?:[-•*·]+|\d{1,2}[.)])\s+")
_NOT_A_LIST = re.compile(r"%|foiz|protsent|kategori|категор")


@dataclass
class Line:
    text: str                           # строка, как её написал владелец (для карточки)
    name: str = ""
    price: float | None = None
    error: str = ""                     # no_price | bad_price — код, текст на языке страницы (assistant.list_error.*)


@dataclass
class Card:
    rows: list = field(default_factory=list)
    skipped: int = 0
    new: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _number(num: str, thousands: str | None) -> float:
    if re.fullmatch(r"\d{1,3}(?:[ .]\d{3})+", num):
        value = float(re.sub(r"[ .]", "", num))
    else:
        value = float(num.replace(",", "."))
    return value * 1000 if thousands else value


def _split(line: str) -> tuple[str, re.Match | None]:
    """Строка -> (название, цена-совпадение или None). Сначала по разделителю («—», «:», табуляция), иначе число в конце."""
    parts = list(_SEPARATOR.finditer(line))
    for sep in reversed(parts):
        price = _PRICE_ONLY.match(line[sep.end():])
        if price and line[:sep.start()].strip():
            return line[:sep.start()].strip(" -—–:="), price
    price = _PRICE_END.search(line)
    if price and price.start() > 0 and not line[price.start() - 1].isspace():
        price = None                    # «Coca-Cola0,33» — число приклеено к названию, это не цена
    if price:
        return line[:price.start()].strip(" -—–:="), price
    return line.strip(" -—–:="), None


def parse(text: str) -> list[Line] | None:
    """Текст владельца -> строки списка или None (это не список «товар — цена», а обычный вопрос для модели)."""
    raw = [_BULLET.sub("", line).strip() for line in re.sub(f"[{_SPACES}]", " ", text or "").splitlines()]
    lines = [line for line in raw if line]
    if len(lines) >= 2 and not re.search(r"\d", lines[0]):
        lines = lines[1:]               # «Новые цены:» / «Narxlar:» — заголовок
    if len(lines) < 2 or "?" in text:
        return None                     # «Покажи товары с ценой 9000? А 8000?» — это вопрос
    out: list[Line] = []
    for line in lines:
        latin = category_words.latin(line)
        if _NOT_A_LIST.search(latin) or _NOT_A_LIST.search(line.lower()) or price_words._direction(latin):   # noqa: SLF001
            return None                 # «подними на 1000», «на 10 %», «категория …» — это не новые цены
        name, price = _split(line)
        item = Line(text=line[:MAX_LINE_CHARS], name=name[:MAX_LINE_CHARS])
        if price is None or not re.search(r"[^\W\d_]{2,}", name):     # «и 8000» — не товар
            item.error = "no_price"
        else:
            try:
                item.price = parse_price(_number(price.group("num"), price.group("k")))
            except (CatalogAddError, ValueError):
                item.error = "bad_price"
        out.append(item)
    priced = sum(1 for item in out if item.error != "no_price")
    if priced < 2 or priced <= len(out) - priced:
        return None
    return out


# --- товар по строке --------------------------------------------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    """«Coca-Cola 0,33 л» -> [coca, cola, 0.33, l]; кириллица и латиница — одними буквами (Фанта = Fanta)."""
    value = category_words.latin(text)
    return re.findall(r"\d+(?:[.,]\d+)?|[a-z]+", value.replace(",", "."))


def _fits(words: list[str], product_words: list[str]) -> bool:
    """Каждое слово строки есть в названии: буквы — начало слова («Pringles orig» -> «Original»), числа — целиком."""
    for w in words:
        if w[0].isdigit():
            short = w.rstrip("0").rstrip(".") if "." in w else w        # «0.50» = «0.5»
            if w not in product_words and short not in product_words:
                return False
        elif not any(p.startswith(w) for p in product_words if not p[0].isdigit()):
            return False
    return True


# --- слово в падеже: «Фанте», «у Фанты», «хлеба», «Fantaning» (задача 3 плана, 2026-11-17, документ 16, §10.13) ----------------
# Владелец пишет название так, как говорит: «Поставь Фанте цену 9500», «Смени бренд у хлеба на Lays», «Fanta narxini …» — а
# в каталоге «Fanta 0,5 л», «Хлеб белый». Слово сравнивается по основе: русское окончание отбрасывается у обоих слов
# («fante» → fant = «fanta» → fant), узбекский суффикс — у слова владельца («fantaning» = fanta + ning). Буквы, которые
# пишут по-разному, сравниваются как одна: «Принц» (prints) = «Prince», «Кока» = «Coca», «Нутела» = «Nutella».
# Это ЗАПАСНОЙ путь: только если обычным сравнением (начало слова) не подошёл ни один товар, и годится только один товар.
_RU_ENDINGS = ("yami", "ami", "yah", "yam", "ov", "ev", "om", "em", "oy", "ey", "ah", "am", "yu", "ya",
               "a", "e", "i", "o", "u", "y")
_WORD_SUFFIXES = {"ning", "ni", "ga", "ka", "qa", "da", "dan", "dagi", "niki", "lar", "larni", "larga", "mi",   # узбекские
                  "a", "u", "e", "om", "y", "i", "ov", "oy", "ah", "ami"}                                  # русские
MIN_STEM = 3


def _sound(word: str) -> str:
    """Буквы, которые пишут по-разному, — одной: ts/c/ck/q → k, w → v, ph → f, двойные — одной."""
    value = word.replace("ts", "c").replace("ck", "k").replace("ph", "f").replace("c", "k").replace("q", "k").replace("w", "v")
    return re.sub(r"(.)\1+", r"\1", value)


def _stem(word: str) -> str:
    for end in _RU_ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= MIN_STEM:
            return word[:-len(end)]
    return word


def _same_word(word: str, product_word: str) -> bool:
    """Слово владельца в падеже (или с узбекским суффиксом) — то же слово названия."""
    w, p = _sound(word), _sound(product_word)
    if len(w) < MIN_STEM or len(p) < MIN_STEM:
        return False
    if w.startswith(p) and w[len(p):] in _WORD_SUFFIXES:
        return True
    return _stem(w) == _stem(p) and len(_stem(w)) >= MIN_STEM


def _fits_stem(words: list[str], product_words: list[str]) -> bool:
    """Как _fits, но буквенное слово годится и по основе (_same_word)."""
    for w in words:
        if w[0].isdigit():
            if not _fits([w], product_words):
                return False
        elif not any(p.startswith(w) or _same_word(w, p) for p in product_words if not p[0].isdigit()):
            return False
    return True


def by_stem(text: str, products: list[Product]) -> list[Product]:
    """Товары, к названию которых слова text подходят по основе (активные — первыми). Для поиска помощника (find_products)."""
    words = _tokens(text)
    if not any(not w[0].isdigit() for w in words):
        return []
    fit = [p for p in products if _fits_stem(words, _tokens(p.name or ""))]
    return sorted(fit, key=lambda p: (p.status or PRODUCT_ACTIVE) != PRODUCT_ACTIVE)


def find(db, name: str, products: list[Product]) -> tuple[str, list[Product]]:
    """Название из строки -> (found | many | new, товары). Точный артикул или штрихкод — сразу; потом точное название;
    потом «все слова строки есть в названии»; если не подошло ничего — то же по основе слова («Фанте» → Fanta, §10.13)."""
    key = name.strip()
    exact = [p for p in products if p.sku == key]
    if not exact:
        codes = search_keys(key)
        if codes:
            ids = {pid for (pid,) in db.query(ProductBarcodeORM.product_id).filter(ProductBarcodeORM.barcode.in_(codes)).all()}
            exact = [p for p in products if p.id in ids]
    words = _tokens(key)
    if not exact and words:
        exact = [p for p in products if _tokens(p.name or "") == words]
        if len(exact) > 1:              # два товара с одним названием — не угадываем
            return "many", exact
    if exact:
        return "found", exact[:1]
    if not words:
        return "new", []
    fit = [p for p in products if _fits(words, _tokens(p.name or ""))] or by_stem(key, products)
    active = [p for p in fit if (p.status or PRODUCT_ACTIVE) == PRODUCT_ACTIVE]
    if len(active) == 1 or (len(fit) == 1):
        return "found", (active or fit)[:1]
    if len(active) > 1:
        return "many", active
    return ("many", fit) if fit else ("new", [])


def card(db, text: str) -> dict | None:
    """Текст владельца -> карточка массового изменения цен (kind=bulk, change=price_list) или None — это не список."""
    lines = parse(text)
    if lines is None:
        return None
    cut = max(0, len(lines) - MAX_LINES)
    lines = lines[:MAX_LINES]
    products = db.query(Product).filter(Product.status.in_((PRODUCT_ACTIVE, PRODUCT_ARCHIVED))).order_by(Product.sku).all()
    out = Card()
    seen: dict[str, str] = {}
    for line in lines:
        if line.error:
            out.errors.append({"line": line.text, "code": line.error})
            continue
        how, found = find(db, line.name, products)
        if how == "new":
            key = "new:" + " ".join(_tokens(line.name))
            if key in seen:                 # новый товар дважды — какая цена верная, не угадываем: кнопки «Добавить» нет
                out.errors.append({"line": line.text, "code": "repeat", "names": [line.name]})
                if seen[key] == "new":
                    out.new = [r for r in out.new if "new:" + " ".join(_tokens(r["name"])) != key]
                    seen[key] = "dropped"
                continue
            seen[key] = "new"
            out.new.append({"line": line.text, "name": line.name, "price": line.price})
            continue
        if how == "many":
            out.errors.append({"line": line.text, "code": "many", "names": [p.name for p in found[:MAX_NAMES]],
                               "more": max(0, len(found) - MAX_NAMES)})
            continue
        product = found[0]
        if (product.status or PRODUCT_ACTIVE) == PRODUCT_ARCHIVED:
            out.errors.append({"line": line.text, "code": "archived", "names": [product.name]})
            continue
        if product.id in seen:
            out.errors.append({"line": line.text, "code": "repeat", "names": [product.name]})
            if seen[product.id] == "row":           # две разные цены одного товара — не угадываем, какая верная
                out.rows = [r for r in out.rows if r["product_id"] != product.id]
                seen[product.id] = "dropped"
            continue
        if product.expected_price == line.price:
            out.skipped += 1
            seen[product.id] = "same"
            continue
        seen[product.id] = "row"
        out.rows.append({"product_id": product.id, "sku": product.sku, "name": product.name, "version": product.version or 1,
                         "before": product.expected_price, "after": line.price})
    return {"kind": "bulk", "field": "price", "change": "price_list", "value": None, "where": "list", "query": "",
            "rows": out.rows, "skipped": out.skipped, "new": out.new, "errors": out.errors, "cut": cut,
            "lines": len(lines)}
