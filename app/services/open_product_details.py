"""
Сведения для карточки покупателя из открытых баз (Open Food / Beauty / Products Facts) — чистые правила, без сети и базы.

Что берётся: вес/объём (quantity), состав (ingredients_text_*), производитель (brand_owner, иначе первый бренд), краткое описание
(generic_name_*), категория (по категориям базы, иначе по словам в названии). Страна — по приставке штрихкода GS1 (страна, где
зарегистрирован производитель: для карточки этого хватает; у импортных товаров с местным штрихкодом может отличаться).

Данные открытых баз внесены покупателями, часто это распознанный текст с упаковки. Состав берётся, только если похож на состав
(не таблица питательности, не обрывки надписей). ПРИМЕРНЫЕ цены (demo_price) — только для показа тестового каталога: настоящих
цен по Узбекистану в открытых базах нет; магазин ставит свои.
"""
from __future__ import annotations

import hashlib
import html
import re

INGREDIENT_LANGS = ("ru", "uz", "en", "")
MAX_INGREDIENTS = 1500
MAX_DESCRIPTION = 300

# Приставки штрихкодов GS1 -> страна (по-русски). Диапазоны включительно; первое совпадение.
_GS1: tuple[tuple[int, int, str], ...] = (
    (0, 19, "США"), (30, 39, "США"), (60, 139, "США"), (300, 379, "Франция"), (380, 380, "Болгария"), (383, 383, "Словения"),
    (385, 385, "Хорватия"), (387, 387, "Босния и Герцеговина"), (400, 440, "Германия"), (450, 459, "Япония"), (460, 469, "Россия"),
    (470, 470, "Кыргызстан"), (471, 471, "Тайвань"), (474, 474, "Эстония"), (475, 475, "Латвия"), (476, 476, "Азербайджан"),
    (477, 477, "Литва"), (478, 478, "Узбекистан"), (479, 479, "Шри-Ланка"), (480, 480, "Филиппины"), (481, 481, "Беларусь"),
    (482, 482, "Украина"), (483, 483, "Туркменистан"), (484, 484, "Молдова"), (485, 485, "Армения"), (486, 486, "Грузия"),
    (487, 487, "Казахстан"), (488, 488, "Таджикистан"), (489, 489, "Гонконг"), (490, 499, "Япония"), (500, 509, "Великобритания"),
    (520, 521, "Греция"), (528, 528, "Ливан"), (529, 529, "Кипр"), (531, 531, "Северная Македония"), (535, 535, "Мальта"),
    (539, 539, "Ирландия"), (540, 549, "Бельгия"), (560, 560, "Португалия"), (569, 569, "Исландия"), (570, 579, "Дания"),
    (590, 590, "Польша"), (594, 594, "Румыния"), (599, 599, "Венгрия"), (600, 601, "ЮАР"), (611, 611, "Марокко"),
    (619, 619, "Тунис"), (621, 621, "Сирия"), (622, 622, "Египет"), (625, 625, "Иордания"), (626, 626, "Иран"),
    (628, 628, "Саудовская Аравия"), (629, 629, "ОАЭ"), (640, 649, "Финляндия"), (690, 699, "Китай"), (700, 709, "Норвегия"),
    (729, 729, "Израиль"), (730, 739, "Швеция"), (760, 769, "Швейцария"), (770, 771, "Колумбия"), (779, 779, "Аргентина"),
    (789, 790, "Бразилия"), (800, 839, "Италия"), (840, 849, "Испания"), (858, 858, "Словакия"), (859, 859, "Чехия"),
    (860, 860, "Сербия"), (865, 865, "Монголия"), (868, 869, "Турция"), (870, 879, "Нидерланды"), (880, 880, "Южная Корея"),
    (885, 885, "Таиланд"), (888, 888, "Сингапур"), (890, 890, "Индия"), (893, 893, "Вьетнам"), (896, 896, "Пакистан"),
    (899, 899, "Индонезия"), (900, 919, "Австрия"), (930, 939, "Австралия"), (940, 949, "Новая Зеландия"), (955, 955, "Малайзия"),
)


def _text(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(html.unescape(str(value or "")))).strip(" ;,.")


def country_from_barcode(code: str) -> str:
    """Страна по приставке GS1 штрихкода EAN-13 (UPC-A — с нулём впереди). Не EAN-13 / внутренний код магазина — пусто."""
    code = str(code or "").strip()
    if len(code) == 12 and code.isdigit():
        code = "0" + code
    if len(code) != 13 or not code.isdigit():
        return ""
    prefix = int(code[:3])
    for lo, hi, country in _GS1:
        if lo <= prefix <= hi:
            return country
    return ""


_UNITS = (
    (r"(?:kg|кг)", "кг"), (r"(?:mg|мг)", "мг"), (r"(?:ml|мл)", "мл"), (r"(?:cl)", "сл"),
    (r"(?:g|gr|gm|grams?|gramm?s?|грамм?|гр|г)", "г"), (r"(?:l|л|lt|ltr|litr|liters?|litres?|литра?|dm³)", "л"),
    (r"(?:pcs|шт|pc|dona|tablets?|pieces|пакетов|пакета|пакет)", "шт"),
)
_QTY = re.compile(r"(\d+(?:[.,]\d+)?)\s*(" + "|".join(u for u, _ in _UNITS) + r")\.?(?![\w])", re.IGNORECASE)


def net_content(quantity: str, name: str = "") -> str:
    """«500 ml» -> «500 мл», «1,5 L» -> «1,5 л»; нет в quantity — ищется в названии. Не нашлось — пусто."""
    for source in (quantity, name):
        m = _QTY.search(_text(source))
        if m:
            number = m.group(1).replace(".", ",")
            unit = next(ru for pattern, ru in _UNITS if re.fullmatch(pattern, m.group(2), re.IGNORECASE))
            return f"{number} {unit}"
    return ""


def amount_in_base(net: str) -> float | None:
    """«500 г» -> 0,5 (кг); «1,5 л» -> 1,5 (л); «330 мл» -> 0,33. Штуки и прочее — None."""
    m = re.fullmatch(r"(\d+(?:,\d+)?) (кг|г|мг|л|мл|сл)", net or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value * {"кг": 1, "г": 0.001, "мг": 0.000001, "л": 1, "мл": 0.001, "сл": 0.01}[m.group(2)]


_NOT_INGREDIENTS = re.compile(r"\b(?:kj|кдж|kcal|ккал|kal/|энергетическ|пищевая ценность|nutrition|срок годности|годен до|"
                              r"saqlash|yaroqlilik|ishlab chiqaruvchi)\b", re.IGNORECASE)


def looks_like_ingredients(text: str) -> bool:
    """Похоже на состав: в основном буквы, есть перечисление, нет таблицы питательности и цифровой каши."""
    text = _text(text)
    if not 8 <= len(text) <= 3000:
        return False
    letters = len(re.findall(r"[^\W\d_]", text))
    digits = len(re.findall(r"\d", text))
    if letters < 0.6 * len(text.replace(" ", "")) or digits > 0.12 * len(text):
        return False
    if _NOT_INGREDIENTS.search(text):
        return False
    return "," in text or ";" in text


def ingredients_of(p: dict) -> str:
    for lang in INGREDIENT_LANGS:
        text = _text(p.get(f"ingredients_text_{lang}" if lang else "ingredients_text"))
        if looks_like_ingredients(text):
            text = re.sub(r"^(?:состав|ingredients|tarkibi)\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
            text = text.replace("_", "")
            if len(text) > MAX_INGREDIENTS:
                text = text[:MAX_INGREDIENTS].rsplit(",", 1)[0] + "…"
            return text[:1].upper() + text[1:]
    return ""


def description_of(p: dict) -> str:
    for lang in ("ru", "uz", "en", ""):
        text = _text(p.get(f"generic_name_{lang}" if lang else "generic_name"))
        if len(re.findall(r"[^\W\d_]", text)) >= 4:
            return (text[:1].upper() + text[1:])[:MAX_DESCRIPTION]
    return ""


def manufacturer_of(p: dict) -> str:
    for value in (p.get("brand_owner"), (str(p.get("brands") or "").split(",") or [""])[0]):
        text = _text(value)
        if len(re.findall(r"[^\W\d_]", text)) >= 2:
            return text[:120]
    return ""


# Товары с ограничением возраста. В Узбекистане алкоголь и табак (и устройства для курения) продают только с 21 года (закон
# 2023 г.; владелец просил «18+», но по закону 21 — docs/CATALOG_SOURCES.md). Категория так и называется, чтобы продавец видел.
ALCOHOL_CATEGORY = "Алкоголь (21+)"
TOBACCO_CATEGORY = "Табак (21+)"
ADULT_CATEGORIES = (ALCOHOL_CATEGORY, TOBACCO_CATEGORY)


def is_adult_category(category: str | None) -> bool:
    """Категория с ограничением возраста: новые названия и старые «Алкоголь»/«Табак» (как в каталогах до 2026-11-17)."""
    low = (category or "").strip().lower()
    return low.startswith("алкоголь") or low.startswith("табак")


# Слова в названии -> категория (русские названия категорий — как в scripts/fetch_open_catalog.RU_KINDS). Первое совпадение.
# Алкоголь и табак — первыми: «шоколадный ликёр» — не сладости, «пиво» — не просто напиток.
_NAME_RULES: tuple[tuple[str, str], ...] = (
    (r"\bпиво\b|\bpivo\b|(?<!ginger )(?<!root )(?<!имбирное )\bbeer\b|\blager\b|\bale\b|\bводка|vodka|\baroq\b|\bвино\b|\bвина\b|\bwine\b|\bvino\b|коньяк|cognac|"
     r"brandy|бренди|виски|whisk|\bром\b|\brum\b|джин\b|\bgin\b|ликёр|ликер|liqueur|шампанск|champagne|игрист|sparkling wine|"
     r"текила|tequila|сидр|\bcider|\bbaltika|балтика|\bsarbast|tuborg|heineken|\bbud\b|corona extra|\balc\.? ?\d|\d ?% ?(?:об|vol)",
     ALCOHOL_CATEGORY),
    (r"сигарет|cigarette|сигар|cigar|табак|tobacco|tamaki|marlboro|winston|\bkent\b|parliament|\bvape|\biqos|heets|"
     r"электронн.* сигарет|\bglo\b|кальян|hookah", TOBACCO_CATEGORY),
    (r"подгузн|diaper|pampers|детск|bolalar|\bbaby\b|малыш", "Детские товары"),
    (r"мороженое|ice ?cream|muzqaymoq|пломбир|заморож|пельмен|frozen", "Заморозка"),
    (r"шампун|shampoo|гель для душа|shower|мыло|\bsoap\b|sovun|зубн|tooth|\bкрем\b|\bcream\b|дезодорант|deodor|лосьон|lotion|"
     r"бальзам|помад|lipstick|туалетн.* бумаг|салфет|прокладк|бритв|razor|парфюм|perfume|маска для", "Косметика и гигиена"),
    (r"порош|detergent|стирк|кондиционер для бель|средство для мыть|fairy|domestos|отбел|\bclean|kir yuvish|чистящ", "Бытовая химия"),
    (r"\bчай\b|\btea\b|\bchoy\b|кофе|coffee|qahva|nescafe|jacobs|капучино|cappuccino", "Чай и кофе"),
    (r"шоколад|chocolate|shokolad|конфет|candy|candies|konfet|печень|biscuit|cookie|pechene|вафл|wafer|\bторт|\bcake|пирож|"
     r"мармелад|жвачк|\bgum\b|зефир|халва|halva|карамел|caramel|snickers|twix|\bmars\b|kit ?kat|bounty|oreo|roshen|milka|"
     r"\bbar\b|батончик|драже|\bирис|пастил|пряник|рулет", "Сладости"),
    (r"молок|\bmilk\b|\bsut\b|кефир|kefir|йогурт|yog(?:h)?urt|творог|\bсыр\b|сырок|cheese|pishloq|сметан|ряженк|qatiq|айран|"
     r"сливк|масло сливоч|butter", "Молочные продукты"),
    (r"\bвода\b|\bwater\b|\bsuv\b|\bсок\b|juice|\bsharbat|лимонад|lemonade|\bcola\b|\bкола\b|coca|pepsi|fanta|sprite|напиток|"
     r"\bdrink|ichimlik|энергет|energy|\bквас|компот|nektar|нектар|tonic|тоник|mineral|минерал", "Напитки"),
    (r"чипс|chips|сухарик|crouton|кириешк|\bорех|\bnuts?\b|арахис|peanut|семечк|seeds|попкорн|popcorn|крекер|cracker|снек|"
     r"snack|pringles|\blays\b|кукурузн.* палоч", "Снеки"),
    (r"кетчуп|ketchup|майонез|mayo|соус|sauce|горчиц|mustard|специ|spice|приправ|seasoning|ziravor|уксус|vinegar|"
     r"\bсоль\b|\bsalt\b|\btuz\b|перец|pepper|паста томат|tomato paste|мёд|мед\b|honey|asal|джем|jam|варенье|"
     r"паста ореховая|nutella|аджик", "Соусы и специи"),
    (r"консерв|canned|тушёнк|тушенк|шпрот|горошек|кукуруза консерв|в собственном соку|маслины|оливки|olive", "Консервы"),
    (r"хлеб|bread|\bnon\b|батон|лаваш|лепёшк|лепешк|булк|сдоб|круассан|croissant|выпечк", "Хлеб и выпечка"),
    (r"колбас|sausage|kolbasa|сосиск|ветчин|\bham\b|\bмясо|\bmeat\b|go'sht|goʻsht|курин|chicken|tovuq|\bрыба|\bfish\b|baliq|"
     r"тунец|\btuna\b|сельд|\bикра", "Мясо и рыба"),
    (r"макарон|\bpasta\b|спагетти|spaghetti|лапш|noodle|\bрис\b|\brice\b|guruch|греч|buckwheat|\bмука\b|flour|\bun\b|крупа|"
     r"овсян|\boats?\b|хлопья|cereal|сахар|sugar|shakar|масло подсолнеч|масло растит|\boil\b|yog'|yogʻ|мюсли|muesli|бульон|"
     r"\bmaggi\b|роллтон|доширак|kasha|каша", "Бакалея"),
    (r"яблок|\bapple|банан|banana|овощ|vegetable|фрукт|fruit|картоф|potato|томат|tomato|огур|cucumber|лимон|\blemon\b",
     "Овощи и фрукты"),
    (r"таблет|tablet|капсул|capsule|сироп от|лекарств|витамин|vitamin|аптечн|pharm|\d ?mg\b|\d ?мг\b|\bbint\b|бинт|пластыр",
     "Лекарства"),
    (r"корм для|pet food|для кошек|для собак|whiskas|pedigree|friskies|kitekat", "Товары для животных"),
    (r"батарейк|battery|зарядк|charger|кабель|\bcable|наушник|earphone|\bлампа|\blamp\b", "Электроника"),
    (r"ручка|\bpen\b|тетрад|notebook|карандаш|pencil|\bклей|\bglue\b|блокнот", "Канцтовары"),
)


def category_from_name(name: str) -> str:
    low = _text(name).lower()
    for pattern, category in _NAME_RULES:
        if re.search(pattern, low):
            return category
    return ""


# ПРИМЕРНЫЕ цены для показа (сумы, конец 2026 г.): (за кг или л, цена без размера, нижняя граница).
_DEMO_PRICES: dict[str, tuple[int, int, int]] = {
    "Напитки": (9_000, 8_000, 3_000), "Молочные продукты": (16_000, 12_000, 4_000), "Сладости": (90_000, 15_000, 3_000),
    "Снеки": (110_000, 14_000, 4_000), "Чай и кофе": (180_000, 35_000, 8_000), "Бакалея": (18_000, 15_000, 5_000),
    "Соусы и специи": (45_000, 16_000, 4_000), "Консервы": (55_000, 22_000, 8_000), "Хлеб и выпечка": (14_000, 5_000, 3_000),
    "Мясо и рыба": (95_000, 45_000, 10_000), "Овощи и фрукты": (15_000, 12_000, 3_000), "Заморозка": (60_000, 25_000, 5_000),
    "Детские товары": (160_000, 60_000, 12_000), "Лекарства": (0, 28_000, 8_000), "Косметика и гигиена": (130_000, 32_000, 9_000),
    "Бытовая химия": (38_000, 30_000, 10_000), TOBACCO_CATEGORY: (0, 26_000, 15_000), "Товары для дома": (0, 40_000, 10_000),
    "Товары для животных": (35_000, 25_000, 6_000), ALCOHOL_CATEGORY: (260_000, 150_000, 25_000),
    "Табак": (0, 26_000, 15_000), "Алкоголь": (260_000, 150_000, 25_000),        # старые названия (каталоги до 2026-11-17)
    "Спортивное питание": (260_000, 200_000, 60_000),
}
_DEFAULT_PRICE = (60_000, 25_000, 5_000)


def demo_price(category: str, net: str, seed: str) -> int:
    """ПРИМЕРНАЯ цена в сумах для показа: по категории и весу/объёму, ±10 % по штрихкоду (чтобы не одинаковые), круглая."""
    per_unit, plain, floor = _DEMO_PRICES.get(category, _DEFAULT_PRICE)
    amount = amount_in_base(net)
    price = per_unit * amount if per_unit and amount else plain
    price = max(floor, min(price, 600_000))
    spread = int(hashlib.sha256(seed.encode()).hexdigest()[:4], 16) / 0xFFFF * 0.2 - 0.1
    price *= 1 + spread
    step = 500 if price < 20_000 else 1_000
    return int(max(step, round(price / step) * step))


def details_for(p: dict, code: str, name: str) -> dict[str, str]:
    """Все сведения, какие удалось найти, одним словарём (пустые — без ключа)."""
    out = {
        "net_content": net_content(str(p.get("quantity") or ""), name),
        "country": country_from_barcode(code),
        "manufacturer": manufacturer_of(p),
        "ingredients": ingredients_of(p),
        "description": description_of(p),
    }
    return {k: v for k, v in out.items() if v}
