"""
Единый список категорий товаров (задание владельца 2026-11-27: «оптимизировать классы» = навести порядок в категориях).

Одна категория = один вид товара (app/services/product_kinds.py): «Сладости» — это вид `sweets`, «Обувь» — `footwear`. Так вид,
который назвала модель по фото, сразу превращается в категорию каталога, а категория — в вид.

В базе категория хранится по-русски (как и раньше: «Сладости»). На странице её подпись переводится на язык покупателя —
словари страниц, ключи `category.<код>` (frontend/i18n.*.js); здесь переводы — для программы (помощник, замеры, тесты).
Свои категории владельца (не из списка) остаются как есть и не переводятся.

Порядок — как у видов: от частых в продуктовом магазине к редким.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.open_product_details import TOBACCO_CATEGORY

DEFAULT_CATEGORY = "Без категории"          # то же, что в catalog_upload / catalog_import


@dataclass(frozen=True)
class Category:
    kind: str       # код вида (product_kinds.KIND_IDS)
    ru: str         # так хранится в базе
    uz: str         # узбекский, латиница
    uz_cyrl: str    # узбекский, кириллица
    en: str


CATEGORIES: tuple[Category, ...] = (
    Category("drink", "Напитки", "Ichimliklar", "Ичимликлар", "Drinks"),
    Category("dairy", "Молочные продукты", "Sut mahsulotlari", "Сут маҳсулотлари", "Dairy"),
    Category("sweets", "Сладости", "Shirinliklar", "Ширинликлар", "Sweets"),
    Category("snack", "Снеки", "Gazaklar", "Газаклар", "Snacks"),
    Category("tea_coffee", "Чай и кофе", "Choy va qahva", "Чой ва қаҳва", "Tea and coffee"),
    Category("grocery", "Бакалея", "Bakaleya", "Бакалея", "Groceries"),
    Category("sauce", "Соусы и специи", "Souslar va ziravorlar", "Соуслар ва зираворлар", "Sauces and spices"),
    Category("canned", "Консервы", "Konservalar", "Консервалар", "Canned food"),
    Category("bakery", "Хлеб и выпечка", "Non va pishiriqlar", "Нон ва пишириқлар", "Bread and bakery"),
    Category("meat_fish", "Мясо и рыба", "Goʻsht va baliq", "Гўшт ва балиқ", "Meat and fish"),
    Category("fruit_veg", "Овощи и фрукты", "Meva va sabzavotlar", "Мева ва сабзавотлар", "Fruit and vegetables"),
    Category("frozen", "Заморозка", "Muzlatilgan mahsulotlar", "Музлатилган маҳсулотлар", "Frozen food"),
    Category("baby", "Детские товары", "Bolalar uchun", "Болалар учун", "Baby products"),
    Category("medicine", "Лекарства", "Dori-darmonlar", "Дори-дармонлар", "Medicine"),
    Category("cosmetics", "Косметика и гигиена", "Kosmetika va gigiyena", "Косметика ва гигиена", "Cosmetics and hygiene"),
    Category("cleaning", "Бытовая химия", "Maishiy kimyo", "Маиший кимё", "Household chemicals"),
    Category("home", "Товары для дома", "Uy-roʻzgʻor buyumlari", "Уй-рўзғор буюмлари", "Household goods"),
    Category("pet", "Товары для животных", "Hayvonlar uchun", "Ҳайвонлар учун", "Pet supplies"),
    Category("clothing", "Одежда", "Kiyim-kechak", "Кийим-кечак", "Clothing"),
    Category("footwear", "Обувь", "Poyabzal", "Пойабзал", "Footwear"),
    Category("bags", "Сумки", "Sumkalar", "Сумкалар", "Bags"),
    Category("accessories", "Аксессуары", "Aksessuarlar", "Аксессуарлар", "Accessories"),
    Category("electronics", "Электроника", "Elektronika", "Электроника", "Electronics"),
    Category("stationery", "Канцтовары", "Kanselyariya mollari", "Канцелярия моллари", "Stationery"),
    Category("toy", "Игрушки", "Oʻyinchoqlar", "Ўйинчоқлар", "Toys"),
    Category("tobacco", TOBACCO_CATEGORY, "Tamaki (21+)", "Тамаки (21+)", "Tobacco (21+)"),
)

RU_BY_KIND: dict[str, str] = {c.kind: c.ru for c in CATEGORIES}
KIND_BY_RU: dict[str, str] = {c.ru: c.kind for c in CATEGORIES}


def category_for_kind(kind: str | None) -> str:
    """Вид товара -> категория каталога («sweets» -> «Сладости»); неизвестный вид -> ""."""
    return RU_BY_KIND.get(kind or "", "")


def kind_of_category(category: str | None) -> str:
    """Категория каталога -> вид («Сладости» -> «sweets»); своя категория владельца или «Без категории» -> ""."""
    return KIND_BY_RU.get((category or "").strip(), "")


def is_known(category: str | None) -> bool:
    """Категория есть и это не «Без категории»: её можно брать как образец (соседи, замер)."""
    value = (category or "").strip()
    return bool(value) and value != DEFAULT_CATEGORY


def label(category: str | None, lang: str) -> str:
    """Подпись категории на языке ru / uz / uz-Cyrl / en; своя категория владельца — как есть."""
    kind = kind_of_category(category)
    if not kind:
        return (category or "").strip()
    item = next(c for c in CATEGORIES if c.kind == kind)
    return {"uz": item.uz, "uz-Cyrl": item.uz_cyrl, "en": item.en}.get(lang, item.ru)
