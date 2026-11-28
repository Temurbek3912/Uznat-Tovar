"""
Разговоры для замера ИИ-помощника (ml/benchmark_assistant.py --set dialogs, замер 2026-11-24). Дополняют 126 отдельных фраз
(ml/assistant_phrases.py): там — выбрана ли верная команда, здесь — то, чего там нет:

  * разговор из 2–3 реплик: уточнение («а фанта?», «поменяй ей цену на …»), «нет, другую»;
  * просьба из двух действий («фанте 9000 и пепси 15000»);
  * ответ по данным: верны ли числа и названия в ТЕКСТЕ ответа, нет ли выдуманной цены;
  * узбекские ответы: алфавит (латиница без кириллицы, кириллица без латиницы), валюта (soʻm / сўм), зацикливание;
  * длинный каталог — копия товаров рабочей базы (~500, похожие названия: семь «Coca-Cola», три «Pepsi», шесть «Lay's»).

Разговор — {"id", "catalog": "small"|"big", "turns": [реплика, …]}; реплика — {"say", "lang", "expect", "says", "not_says"}:
  expect   — как в assistant_phrases (read:…, upd:…, status:…, bulk:…, lang:…, theme:…, none; варианты через «|»), плюс
             «=значение» — значение в карточке («upd:NUT:price=47000») и «&» — нужны ВСЕ части («lang:uz&theme:dark»);
  says     — что должно быть в тексте ответа: список, каждый элемент — варианты через «|» (цифры сравниваются без пробелов);
  not_says — чего в ответе быть не должно («re:…» — регулярное выражение).
В says/not_says «{price:SKU}» — цена товара каталога замера.
Ответ помощника идёт в историю, как на странице: его текст, а если текста нет (карточка, смена языка) — текст страницы.
"""
from __future__ import annotations

import zlib

# --- длинный каталог: копия рабочей базы (без алкоголя и табака), цены замера --------------------------------------------------
# В рабочей базе цен нет — для замера они ставятся одинаково при каждом прогоне: у названных ниже товаров — свои, у остальных
# 8 из 10 — по контрольной сумме артикула, 2 из 10 — без цены.
BIG_PRICES = {
    "UZ-4780069000130": 9000,       # Coca-Cola Classic 0,5 л
    "UZ-4780069000154": 14000,      # Coca-Cola Classic 1,5 л
    "UZ-5449000000286": 17000,      # Coca-Cola 2 л
    "UZ-0049000061048": 6000,       # Coca-Cola Zero 222ml
    "UZ-5449000231659": 11000,      # Coca Cola zero 1litr
    "UZ-4780022620177": 13000,      # Pepsi 1.5
    "UZ-4780022620153": 8000,       # Pepsi
    "UZ-4870001570071": 10000,      # «Pepsi-Cola» 1 л
    "UZ-4870071000232": 4000,       # Tassay 1,5 л
    "UZ-4870071000195": 2500,       # Tassay 0,5 л
    "UZ-5941000025615": 15000,      # Lay's сыр 140 г
    "UZ-5941000025608": 15000,      # Lay's барбекю 140 г
    "UZ-4601576007231": 12000,      # Кетчуп 350 г ЕЖК
    "UZ-4601674008642": 18000,      # Томатный кетчуп 350 g Heinz
    "UZ-4870206412442": 11000,      # Кефир 2,5 % 930 g Простоквашино
    "UZ-5000159443630": 32000,      # Twix minis 184 g
    "UZ-90495816": 5000,            # Sprite 250ml
    "UZ-2010002612109": None,       # Хлеб белый «Особый» — без цены
}


def big_price(sku: str):
    if sku in BIG_PRICES:
        return BIG_PRICES[sku]
    c = zlib.crc32(sku.encode())
    return None if c % 10 >= 8 else 2000 + (c // 10 % 80) * 500


def _t(say: str, lang: str, expect: str, says=(), not_says=()) -> dict:
    return {"say": say, "lang": lang, "expect": expect, "says": list(says), "not_says": list(not_says)}


_NO_PRICE_RU = "re:\\d{3,}\\s*(?:сум|сом|тенге)"           # выдуманная цена в ответе
_NO_PRICE_UZ = "re:\\d{3,}\\s*(?:soʻm|so'm|som|сўм|сум|сом)"

DIALOGS: list[dict] = [
    # --- уточнение в разговоре (маленький каталог ml/assistant_phrases.CATALOG)
    {"id": "follow_price_ru", "catalog": "small", "turns": [
        _t("Сколько стоит пепси?", "ru", "read:find_products", ["14000"]),
        _t("а фанта?", "ru", "read:find_products", ["8500"])]},
    {"id": "follow_set_price_ru", "catalog": "small", "turns": [
        _t("Есть у нас пепси?", "ru", "read:find_products", ["pepsi|пепси"]),
        _t("поменяй её цену на 15000", "ru", "upd:PEPSI:price=15000")]},
    {"id": "follow_raise_ru", "catalog": "small", "turns": [
        _t("Сколько стоит нутелла?", "ru", "read:find_products", ["45000"]),
        _t("подними на 2000", "ru", "upd:NUT:price=47000")]},
    {"id": "follow_same_ru", "catalog": "small", "turns": [
        _t("Поставь цену фанты 9000", "ru", "upd:FANTA:price=9000"),
        _t("и кока-коле тоже 9500", "ru", "upd:COLA:price=9500")]},
    {"id": "follow_not_that_ru", "catalog": "small", "turns": [
        _t("Убери пепси в архив", "ru", "status:PEPSI:archived"),
        _t("нет, не пепси, а фанту", "ru", "status:FANTA:archived")]},
    {"id": "follow_missing_ru", "catalog": "small", "turns": [
        _t("Какие товары без цены?", "ru", "read:products_missing", ["молоко|nestle"]),
        _t("поставь ему цену 12000", "ru", "upd:MILK:price=12000")]},
    {"id": "follow_brand_ru", "catalog": "small", "turns": [
        _t("Сколько стоит сникерс?", "ru", "read:find_products", ["7000"]),
        _t("а какой у него бренд?", "ru", "read:find_products|read:product_details|none", ["mars"])]},
    {"id": "follow_count_ru", "catalog": "small", "turns": [
        _t("Сколько товаров без фото?", "ru", "read:products_missing|read:catalog_overview|read:count_products", ["10"]),
        _t("а без штрихкода?", "ru", "read:products_missing|read:catalog_overview", ["7"])]},
    {"id": "follow_theme_ru", "catalog": "small", "turns": [
        _t("Сделай тёмный фон", "ru", "theme:dark"),
        _t("нет, верни светлый", "ru", "theme:light")]},
    {"id": "follow_bulk_ru", "catalog": "small", "turns": [
        _t("Подними цены на все напитки на 10%", "ru", "bulk:price_percent:category"),
        _t("нет, не напитки, а все сладости", "ru", "bulk:price_percent:category")]},
    {"id": "follow_lang_switch", "catalog": "small", "turns": [
        _t("Переключи на узбекский", "ru", "lang:uz"),
        _t("Pepsi narxi qancha?", "uz", "read:find_products", ["14000"])]},
    {"id": "follow_price_uz", "catalog": "small", "turns": [
        _t("Pepsi narxi qancha?", "uz", "read:find_products", ["14000"]),
        _t("Fanta-chi?", "uz", "read:find_products", ["8500"])]},
    {"id": "follow_set_price_uz", "catalog": "small", "turns": [
        _t("Nutella bormi?", "uz", "read:find_products", ["nutella"]),
        _t("narxini 50000 qiling", "uz", "upd:NUT:price=50000")]},
    {"id": "follow_set_price_uzc", "catalog": "small", "turns": [
        _t("Пепси борми?", "uz-Cyrl", "read:find_products", ["pepsi|пепси"]),
        _t("нархини 15000 қилинг", "uz-Cyrl", "upd:PEPSI:price=15000")]},
    {"id": "follow_archive_uz", "catalog": "small", "turns": [
        _t("Snickers qancha turadi?", "uz", "read:find_products", ["7000"]),
        _t("uni arxivga oling", "uz", "status:SNICK:archived")]},
    {"id": "follow_three_ru", "catalog": "small", "turns": [
        _t("Сколько стоит вода?", "ru", "read:find_products", ["4000"]),
        _t("а хлеб?", "ru", "read:find_products", ["4000"]),
        _t("хлебу поставь 4500", "ru", "upd:BREAD:price=4500")]},
    # --- два действия в одной просьбе
    {"id": "two_prices_ru", "catalog": "small", "turns": [
        _t("Поставь цену фанты 9000 и пепси 15000", "ru", "upd:FANTA:price=9000&upd:PEPSI:price=15000")]},
    {"id": "two_archive_ru", "catalog": "small", "turns": [
        _t("Убери в архив пепси и сникерс", "ru", "status:PEPSI:archived&status:SNICK:archived")]},
    {"id": "two_ask_ru", "catalog": "small", "turns": [
        _t("Сколько стоят пепси и фанта?", "ru", "read:find_products", ["14000", "8500"])]},
    {"id": "two_ask_uz", "catalog": "small", "turns": [
        _t("Pepsi va Fanta narxi qancha?", "uz", "read:find_products", ["14000", "8500"])]},
    {"id": "two_ui_ru", "catalog": "small", "turns": [
        _t("Переключи на узбекский и сделай тёмный фон", "ru", "lang:uz&theme:dark")]},
    # --- ответ по данным: числа и названия
    {"id": "data_count_ru", "catalog": "small", "turns": [
        _t("Сколько у меня товаров?", "ru", "read:catalog_overview|read:count_products", ["10"])]},
    {"id": "data_archive_ru", "catalog": "small", "turns": [
        _t("Сколько товаров в архиве?", "ru", "read:catalog_overview|read:count_products", ["1"])]},
    {"id": "data_cat_ru", "catalog": "small", "turns": [
        _t("Сколько товаров в категории напитки?", "ru", "read:catalog_overview|read:find_products|read:count_products", ["4"])]},
    {"id": "data_water_ru", "catalog": "small", "turns": [
        _t("Сколько стоит вода?", "ru", "read:find_products", ["4000"])]},
    {"id": "data_noprice_ru", "catalog": "small", "turns": [
        _t("Сколько стоит молоко?", "ru", "read:find_products", ["нет|не указ|не задан|не устан"], [_NO_PRICE_RU])]},
    {"id": "data_absent_ru", "catalog": "small", "turns": [
        _t("Сколько стоит кефир?", "ru", "read:find_products", ["нет|не най"], [_NO_PRICE_RU])]},
    {"id": "data_which_noprice_ru", "catalog": "small", "turns": [
        _t("У какого товара нет цены?", "ru", "read:products_missing", ["молоко|nestle"])]},
    {"id": "data_expensive_ru", "catalog": "small", "turns": [
        _t("Какой у нас самый дорогой товар?", "ru", "read:find_products|read:catalog_overview|none",
           ["nutella|нутелла|не могу|нет"], ["re:(?i)pringles.{0,40}сам|самый дорогой.{0,40}pringles"])]},
    {"id": "data_count_uz", "catalog": "small", "turns": [
        _t("Nechta tovar bor?", "uz", "read:catalog_overview|read:count_products", ["10"])]},
    {"id": "data_price_uz", "catalog": "small", "turns": [
        _t("Nutella qancha turadi?", "uz", "read:find_products", ["45000"])]},
    {"id": "data_price_uzc", "catalog": "small", "turns": [
        _t("Нутелла қанча туради?", "uz-Cyrl", "read:find_products", ["45000"])]},
    {"id": "data_noprice_uz", "catalog": "small", "turns": [
        _t("Sut qancha turadi?", "uz", "read:find_products",
           ["yoʻq|yo'q|kiritilmagan|belgilanmagan|koʻrsatilmagan|topilmad|mavjud emas"],
           [_NO_PRICE_UZ])]},
    {"id": "data_missing_uz", "catalog": "small", "turns": [
        _t("Qaysi tovarlarning narxi yoʻq?", "uz", "read:products_missing", ["nestle|молоко"])]},
    {"id": "data_count_uzc", "catalog": "small", "turns": [
        _t("Каталогда қанча маҳсулот бор?", "uz-Cyrl", "read:catalog_overview|read:count_products", ["10"])]},
    {"id": "data_barcode_uzc", "catalog": "small", "turns": [
        _t("Штрихкоди йўқ товарлар нечта?", "uz-Cyrl", "read:products_missing|read:catalog_overview|read:count_products", ["7"])]},
    {"id": "data_cats_uz", "catalog": "small", "turns": [
        _t("Qanday kategoriyalar bor?", "uz", "read:catalog_overview", ["напитки|ichimlik", "сладости|shirinlik"])]},
    # --- длинный каталог: копия рабочей базы
    {"id": "big_count", "catalog": "big", "turns": [
        _t("Сколько у меня товаров?", "ru", "read:catalog_overview|read:count_products", ["495"])]},
    {"id": "big_nocat", "catalog": "big", "turns": [
        _t("Сколько товаров без категории?", "ru", "read:products_missing|read:catalog_overview|read:count_products", ["37"])]},
    {"id": "big_cola05", "catalog": "big", "turns": [
        _t("Сколько стоит Coca-Cola 0,5?", "ru", "read:find_products", ["9000"])]},
    {"id": "big_cola15_set", "catalog": "big", "turns": [
        _t("Поставь цену Coca-Cola Classic 1,5 л 15000", "ru", "upd:UZ-4780069000154:price=15000")]},
    {"id": "big_cola2_follow", "catalog": "big", "turns": [
        _t("Сколько стоит Coca-Cola 2 л?", "ru", "read:find_products", ["17000"]),
        _t("поставь ей 18000", "ru", "upd:UZ-5449000000286:price=18000")]},
    {"id": "big_pepsi_ambiguous", "catalog": "big", "turns": [
        _t("Поставь цену пепси 12000", "ru", "none|upd:UZ-4780022620153:price=12000|upd:UZ-4780022620177:price=12000|"
           "upd:UZ-4870001570071:price=12000")]},
    {"id": "big_pepsi15_archive", "catalog": "big", "turns": [
        _t("Убери в архив Pepsi 1.5", "ru", "status:UZ-4780022620177:archived")]},
    {"id": "big_lays_cheese", "catalog": "big", "turns": [
        _t("Lay's сыр сколько стоит?", "ru", "read:find_products", ["15000"])]},
    {"id": "big_tassay_uz", "catalog": "big", "turns": [
        _t("Tassay 1,5 litr narxi qancha?", "uz", "read:find_products", ["4000"])]},
    {"id": "big_kefir", "catalog": "big", "turns": [
        _t("Кефир Простоквашино сколько стоит?", "ru", "read:find_products", ["11000"])]},
    {"id": "big_ketchup_rename", "catalog": "big", "turns": [
        _t("Переименуй Кетчуп 350 г в Кетчуп ЕЖК 350 г", "ru", "upd:UZ-4601576007231:name")]},
    {"id": "big_heinz_bulk", "catalog": "big", "turns": [
        _t("Подними цены на все товары Heinz на 10%", "ru", "bulk:price_percent:brand")]},
    {"id": "big_twix_uzc", "catalog": "big", "turns": [
        _t("Twix minis қанча туради?", "uz-Cyrl", "read:find_products", ["32000"])]},
    {"id": "big_bread_noprice", "catalog": "big", "turns": [
        _t("Сколько стоит хлеб белый Особый?", "ru", "read:find_products", ["нет|не указ|не задан|не устан"],
           [_NO_PRICE_RU])]},
    {"id": "big_sprite_follow", "catalog": "big", "turns": [
        _t("Sprite bormi?", "uz", "read:find_products", ["sprite"]),
        _t("narxini 5500 qiling", "uz", "upd:UZ-90495816:price=5500")]},
    {"id": "big_cola_count", "catalog": "big", "turns": [
        _t("Какие у нас есть кока-колы?", "ru", "read:find_products", ["0,5|0.5", "1,5|1.5"])]},
]
