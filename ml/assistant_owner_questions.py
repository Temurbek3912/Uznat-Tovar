"""
Вопросы и просьбы владельца к ИИ-помощнику — всё, что он может спросить или попросить (ml/benchmark_assistant.py --set owner,
замер 2026-11-28). Дополняют прежние наборы (ml/assistant_phrases — 126 фраз, ml/assistant_dialogs — разговоры,
ml/assistant_counts — «сколько»): здесь тех же видов вопросов больше и на всех четырёх языках программы (ru, uz — латиница,
uz-Cyrl — кириллица, en — английский; в прежних наборах английского не было), с разговорными написаниями и ошибками, плюс:

  * копировать товар — такого действия у помощника нет: верно, если он честно не делает (или предлагает новый товар), а не
    выдумывает «скопировано»;
  * «Сделать» и «Отменить»: в разговорах с "apply" карточка применяется на временной базе (как кнопкой на странице, через
    app/api/routes_assistant.assistant_act), потом проверяется, что в базе стало ожидаемое ("db"), а после «Отменить»
    ("undo") — что вернулось прежнее. Рабочая база не трогается.

Формат — как ml/assistant_dialogs: {"id", "catalog": "small", "turns": [...]}; id начинается с вида вопроса (find_, count_, …) —
по нему итог делится на виды. Реплика — _t(...) из ml/assistant_dialogs плюс необязательные:
  db   — {артикул: {поле: значение}} после «Сделать» (поля: price, name, brand, category, in_stock, status, barcode — код есть);
  undo — True: после проверки db нажать «Отменить» и проверить, что поля вернулись;
  card — {поле: часть значения} в карточке нового товара (name — без регистра; price — число).
Каталог — маленький (ml/assistant_phrases.CATALOG): COLA 9000, FANTA 8500, PEPSI 14000 (без штрихкода), WATER 4000, NUT 45000,
SNICK 7000, PRING 32000, LAYS 12000, BREAD 4000 (бренд «Не указан»), MILK без цены; в архиве JUICE (сок Добрый) 15000.
"""
from __future__ import annotations

from ml.assistant_dialogs import _NO_PRICE_RU, _NO_PRICE_UZ, _t


def T(say: str, lang: str, expect: str, says=(), not_says=(), db=None, undo=False, card=None) -> dict:
    turn = _t(say, lang, expect, says, not_says)
    if db:
        turn["db"] = db
    if undo:
        turn["undo"] = True
    if card:
        turn["card"] = card
    return turn


def _one(kind: str, n: int, turn: dict, apply: bool = False) -> dict:
    d = {"id": f"{kind}_{turn['lang']}_{n}", "catalog": "small", "turns": [turn]}
    if apply:
        d["apply"] = True
    return d


def _many(kind: str, turns: list[dict], apply_all: bool = False) -> list[dict]:
    return [_one(kind, n, t, apply_all or bool(t.get("db"))) for n, t in enumerate(turns, 1)]


_ABSENT_RU = "нет|не най|не нашл|отсутств"
_ABSENT_UZ = "yoʻq|yo'q|topilmad|mavjud emas|yoq"
_ABSENT_UZC = "йўқ|топилмад|мавжуд эмас"
_ABSENT_EN = "no |not |n't|none"
_COPY_CLAIM = "re:(?i)скопирова(?:л|н)|\\bcopied\\b|nusxa(?:si)? (?:olindi|yaratildi)|нусха(?:си)? (?:олинди|яратилди)"
_NO_PRICE_EN = "re:\\d{3,}\\s*(?:uzs|sum|som)"
_FIND = "read:find_products"
_PROD = "read:find_products|read:product_details"
_COUNT = "read:count_products|read:catalog_overview|read:products_missing|read:find_products"
_MISS = "read:products_missing"

FIND = _many("find", [
    T("Почём у нас пепси?", "ru", _FIND, ["14000"]),
    T("есть ли спрайт", "ru", _FIND, [_ABSENT_RU], [_NO_PRICE_RU]),
    T("скока стоет фанта", "ru", _FIND, ["8500"]),
    T("Что за товар со штрихкодом 4780001234579?", "ru", _PROD, ["fanta|фанта"]),
    T("Какая цена у воды?", "ru", _FIND, ["4000"]),
    T("Coca-Cola narxi qancha?", "uz", _FIND, ["9000"]),
    T("Sprite bormi?", "uz", _FIND, [_ABSENT_UZ], [_NO_PRICE_UZ]),
    T("kokakola qancha", "uz", _FIND, ["9000"]),
    T("Lays narxi necha pul?", "uz", _FIND, ["12000"]),
    T("Кока-кола нархи қанча?", "uz-Cyrl", _FIND, ["9000"]),
    T("Сникерс борми?", "uz-Cyrl", _FIND, ["snickers|сникерс"]),
    T("Спрайт борми?", "uz-Cyrl", _FIND, [_ABSENT_UZC], [_NO_PRICE_UZ]),
    T("Принглс неча пул?", "uz-Cyrl", _FIND, ["32000"]),
    T("How much is Pepsi?", "en", _FIND, ["14000"]),
    T("Do we have Nutella?", "en", _FIND, ["nutella"]),
    T("price of fanta", "en", _FIND, ["8500"]),
    T("Do we sell Sprite?", "en", _FIND, [_ABSENT_EN], [_NO_PRICE_EN]),
    T("What product has barcode 8000500310427?", "en", _PROD, ["nutella"]),
    T("how much does the milk cost", "en", _FIND, ["no price|not set|no |not |isn't|hasn't"], [_NO_PRICE_EN]),
])

COUNT = _many("count", [
    T("сколько у нас снеков", "ru", _COUNT, ["2"]),
    T("Сколько товаров Coca-Cola?", "ru", _COUNT, ["2"]),
    T("Сколько товаров дороже 10000?", "ru", _COUNT, ["4"]),
    T("Nechta ichimlik bor?", "uz", _COUNT, ["4"]),
    T("Arxivda nechta tovar bor?", "uz", _COUNT, ["1"]),
    T("Нечта ширинлик бор?", "uz-Cyrl", _COUNT, ["2"]),
    T("Нархи йўқ товарлар нечта?", "uz-Cyrl", _COUNT, ["1"]),
    T("How many products do we have?", "en", _COUNT, ["10"]),
    T("How many drinks do we have?", "en", _COUNT, ["4"]),
    T("How many products have no price?", "en", _COUNT, ["1"]),
    T("How many products are in the archive?", "en", _COUNT, ["1"]),
    T("how many snacks", "en", _COUNT, ["2"]),
])

MISSING = _many("missing", [
    T("у чего нет фоток", "ru", _MISS),
    T("Покажи товары без бренда", "ru", _MISS, ["хлеб"]),
    T("Brendi yoʻq tovarlar", "uz", _MISS, ["хлеб|non"]),
    T("Narxi kiritilmagan tovarlar qaysi?", "uz", _MISS, ["nestle|молоко"]),
    T("Штрихкоди йўқ товарлар қайсилар?", "uz-Cyrl", _MISS),
    T("Бренди йўқ товарлар", "uz-Cyrl", _MISS, ["хлеб|нон"]),
    T("Which products have no photo?", "en", _MISS),
    T("Show products without barcode", "en", _MISS),
    T("Products with no price?", "en", _MISS, ["nestle|молоко|milk"]),
    T("which items have no brand", "en", _MISS, ["хлеб|bread"]),
])

OTHER_READ = _many("dup", [
    T("дубликаты есть?", "ru", "read:similar_names"),
    T("Takroriy tovarlar bormi?", "uz", "read:similar_names"),
    T("Бир хил товарлар борми?", "uz-Cyrl", "read:similar_names"),
    T("Are there duplicate products?", "en", "read:similar_names"),
]) + _many("scan", [
    T("Какие товары плохо узнаются?", "ru", "read:scan_problems"),
    T("Qaysi tovarlar yomon taniladi?", "uz", "read:scan_problems"),
    T("Қайси товарлар ёмон танилади?", "uz-Cyrl", "read:scan_problems"),
    T("Why doesn't the program recognize products?", "en", "read:scan_problems"),
]) + _many("staff", [
    T("сколько у меня сотрудников", "ru", "read:staff_list"),
    T("Kim ishlaydi?", "uz", "read:staff_list"),
    T("Ходимлар кимлар?", "uz-Cyrl", "read:staff_list"),
    T("Who works for me?", "en", "read:staff_list"),
    T("Show the staff list", "en", "read:staff_list"),
]) + _many("backup", [
    T("копия базы когда была", "ru", "read:backup_status"),
    T("Oxirgi zaxira nusxa qachon?", "uz", "read:backup_status"),
    T("Охирги заҳира нусха қачон бўлган?", "uz-Cyrl", "read:backup_status"),
    T("When was the last backup?", "en", "read:backup_status"),
])

ADD = _many("add", [
    T("Добавь товар Спрайт 1 л, цена 11000, бренд Coca-Cola, категория Напитки", "ru", "add",
      card={"name": "спрайт", "price": 11000}),
    T("заведи новый товар Чай Ahmad 100 г за 25000", "ru", "add", card={"name": "ahmad", "price": 25000}),
    T("Yangi mahsulot qoʻshing: Chips Lays 150 g, 15000 soʻm", "uz", "add", card={"name": "lays", "price": 15000}),
    T("Янги товар қўшинг: Сут Nestle 0,5 л, нархи 7000", "uz-Cyrl", "add", card={"name": "nestle", "price": 7000}),
    T("Add a new product Sprite 0.5 l for 8500", "en", "add", card={"name": "sprite", "price": 8500}),
    T("Create product Mars 50 g, price 7000", "en", "add", card={"name": "mars", "price": 7000}),
])

UPDATE = _many("upd", [
    # цена
    T("Измени цену кока-колы на 9500", "ru", "upd:COLA:price=9500", db={"COLA": {"price": 9500}}, undo=True),
    T("Нутелла теперь стоит 46 тысяч", "ru", "upd:NUT:price=46000", db={"NUT": {"price": 46000}}),
    T("фанта подорожала на 500", "ru", "upd:FANTA:price=9000", db={"FANTA": {"price": 9000}}),
    T("сделай сникерс подешевле на 1000", "ru", "upd:SNICK:price=6000"),
    T("Coca-Cola narxini 9500 soʻm qiling", "uz", "upd:COLA:price=9500", db={"COLA": {"price": 9500}}),
    T("Nutella narxini 2000 ga oshiring", "uz", "upd:NUT:price=47000"),
    T("Кока-кола нархини 9500 қилинг", "uz-Cyrl", "upd:COLA:price=9500", db={"COLA": {"price": 9500}}),
    T("Нутелла нархини 1000 га камайтиринг", "uz-Cyrl", "upd:NUT:price=44000"),
    T("Set the price of Coca-Cola to 9500", "en", "upd:COLA:price=9500", db={"COLA": {"price": 9500}}, undo=True),
    T("Change Nutella price to 46000", "en", "upd:NUT:price=46000"),
    T("Raise the price of Fanta by 500", "en", "upd:FANTA:price=9000"),
    # название
    T("переименуй Pepsi 1,5 л в Pepsi Cola 1,5 л", "ru", "upd:PEPSI:name=Pepsi Cola 1,5 л",
      db={"PEPSI": {"name": "Pepsi Cola 1,5 л"}}, undo=True),
    T("Pepsi nomini Pepsi Cola 1,5 l ga oʻzgartiring", "uz", "upd:PEPSI:name"),
    T("Пепси номини Pepsi Cola қилинг", "uz-Cyrl", "upd:PEPSI:name"),
    T("Rename Pepsi 1.5 l to Pepsi Cola 1.5 l", "en", "upd:PEPSI:name"),
    # бренд
    T("у фанты бренд поменяй на Fanta", "ru", "upd:FANTA:brand=Fanta", db={"FANTA": {"brand": "Fanta"}}),
    T("Snickers brendini Mars Inc qiling", "uz", "upd:SNICK:brand=Mars Inc"),
    T("Сникерс брендини Mars Inc қилинг", "uz-Cyrl", "upd:SNICK:brand=Mars Inc"),
    T("Set brand of Snickers to Mars Inc", "en", "upd:SNICK:brand=Mars Inc"),
    # категория
    T("Сникерс перенеси в категорию Шоколад", "ru", "upd:SNICK:category", db={"SNICK": {"category": "Шоколад"}}),
    T("Snickers kategoriyasini Shokolad qiling", "uz", "upd:SNICK:category"),
    T("Сникерс категориясини Шоколад қилинг", "uz-Cyrl", "upd:SNICK:category"),
    T("Move Hydrolife water to category Water", "en", "upd:WATER:category"),
    # штрихкод
    T("Добавь пепси штрихкод 4870001234560", "ru", "upd:PEPSI:add_barcode",
      db={"PEPSI": {"barcode": "4870001234560"}}, undo=True),
    T("Pepsiga 4870001234560 shtrixkodini qoʻshing", "uz", "upd:PEPSI:add_barcode"),
    T("Пепсига 4870001234560 штрихкодини қўшинг", "uz-Cyrl", "upd:PEPSI:add_barcode"),
    T("Add barcode 4870001234560 to Pepsi", "en", "upd:PEPSI:add_barcode"),
    T("убери штрихкод у фанты", "ru", "upd:FANTA:remove_barcode|none"),
    # наличие
    T("молоко снова есть в наличии", "ru", "upd:MILK:in_stock"),
    T("Pepsi is out of stock", "en", "upd:PEPSI:in_stock", db={"PEPSI": {"in_stock": False}}),
    T("Pepsi qolmadi", "uz", "upd:PEPSI:in_stock"),
    T("Пепси тугади", "uz-Cyrl", "upd:PEPSI:in_stock"),
])

ARCHIVE = _many("arch", [
    T("Удали товар Lay's", "ru", "status:LAYS:archived", db={"LAYS": {"status": "archived"}}, undo=True),
    T("Пепси больше не продаём", "ru", "status:PEPSI:archived"),
    T("Сок Добрый снова продаём, верни", "ru", "status:JUICE:active", db={"JUICE": {"status": "active"}}),
    T("Удали насовсем нутеллу", "ru", "status:NUT:archived"),
    T("Lay'sni oʻchirib tashlang", "uz", "status:LAYS:archived"),
    T("Pringlesni arxivga oling", "uz", "status:PRING:archived", db={"PRING": {"status": "archived"}}),
    T("Dobriy sharbatni qaytaring", "uz", "status:JUICE:active"),
    T("Сникерсни ўчиринг", "uz-Cyrl", "status:SNICK:archived"),
    T("Добрый шарбатини архивдан қайтаринг", "uz-Cyrl", "status:JUICE:active"),
    T("Delete Snickers", "en", "status:SNICK:archived", db={"SNICK": {"status": "archived"}}),
    T("Archive Pepsi", "en", "status:PEPSI:archived"),
    T("Restore Dobriy juice from the archive", "en", "status:JUICE:active"),
    T("Put Lay's in the archive", "en", "status:LAYS:archived"),
])

# Копировать товар: такого действия нет. Верно — честно не сделать (или предложить новый товар); «скопировано» — неверно.
COPY = _many("copy", [
    T("Скопируй товар фанта", "ru", "none|add", [], [_COPY_CLAIM]),
    T("Сделай копию нутеллы, только 750 г", "ru", "none|add", [], [_COPY_CLAIM]),
    T("Fanta tovaridan nusxa oling", "uz", "none|add", [], [_COPY_CLAIM]),
    T("Фанта товаридан нусха олинг", "uz-Cyrl", "none|add", [], [_COPY_CLAIM]),
    T("Duplicate the Fanta product", "en", "none|add", [], [_COPY_CLAIM]),
    T("Copy Nutella as a new product", "en", "none|add", [], [_COPY_CLAIM]),
])

BULK = _many("bulk", [
    T("Подними цены на все снеки на 1000", "ru", "bulk:price_add:category=Снеки",
      db={"PRING": {"price": 33000}, "LAYS": {"price": 13000}}, undo=True),
    T("Снизь цены на все сладости на 10%", "ru", "bulk:price_percent:category=Сладости",
      db={"NUT": {"price": 40500}, "SNICK": {"price": 6300}}),
    T("Поставь всем напиткам цену 10000", "ru", "bulk:price_set:category=Напитки",
      db={"COLA": {"price": 10000}, "WATER": {"price": 10000}}),
    T("Всем товарам Coca-Cola поставь категорию Газировка", "ru", "bulk:category:brand=Coca-Cola",
      db={"COLA": {"category": "Газировка"}, "FANTA": {"category": "Газировка"}}),
    T("Поменяй бренд у всех снеков на Snack Co", "ru", "bulk:brand:category"),
    T("Верни из архива все напитки", "ru", "bulk:restore:category", db={"JUICE": {"status": "active"}}),
    T("Все напитки закончились", "ru", "bulk:in_stock:category"),
    T("Убери в архив всё от Pepsi", "ru", "bulk:archive:brand|status:PEPSI:archived"),
    T("Barcha shirinliklar narxini 1000 soʻmga oshiring", "uz", "bulk:price_add:category=Сладости",
      db={"NUT": {"price": 46000}, "SNICK": {"price": 8000}}),
    T("Coca-Cola mahsulotlari narxini 5% ga tushiring", "uz", "bulk:price_percent:brand"),
    T("Barcha gazaklarni Chipslar kategoriyasiga oʻtkazing", "uz", "bulk:category:category"),
    T("Барча ширинликлар нархини 10% га оширинг", "uz-Cyrl", "bulk:price_percent:category=Сладости"),
    T("Барча газакларни архивга олинг", "uz-Cyrl", "bulk:archive:category=Снеки"),
    T("Барча Coca-Cola товарлари нархини 500 сўмга оширинг", "uz-Cyrl", "bulk:price_add:brand"),
    T("Raise prices of all drinks by 10%", "en", "bulk:price_percent:category=Напитки",
      db={"COLA": {"price": 9900}, "WATER": {"price": 4400}}),
    T("Increase prices of all snacks by 1000", "en", "bulk:price_add:category=Снеки"),
    T("Give a 5% discount on all Coca-Cola products", "en", "bulk:price_percent:brand"),
    T("Archive all snacks", "en", "bulk:archive:category=Снеки"),
    T("Set category Soda for all Coca-Cola products", "en", "bulk:category:brand"),
    T("Set price 10000 for all sweets", "en", "bulk:price_set:category=Сладости"),
])

LIST = _many("list", [
    T("Цены:\nфанта 9000\nпепси 15000\nнутелла 46000", "ru",
      "upd:FANTA:price=9000&upd:PEPSI:price=15000&upd:NUT:price=46000",
      db={"FANTA": {"price": 9000}, "PEPSI": {"price": 15000}, "NUT": {"price": 46000}}, undo=True),
    T("сникерсу 7500, лейс 12500", "ru", "upd:SNICK:price=7500&upd:LAYS:price=12500"),
    T("Narxlar:\nFanta - 9000\nPepsi - 15000", "uz", "upd:FANTA:price=9000&upd:PEPSI:price=15000",
      db={"FANTA": {"price": 9000}, "PEPSI": {"price": 15000}}),
    T("Фанта 9000\nПепси 15000", "uz-Cyrl", "upd:FANTA:price=9000&upd:PEPSI:price=15000"),
    T("New prices:\nFanta 9000\nPepsi 15000", "en", "upd:FANTA:price=9000&upd:PEPSI:price=15000"),
])

UI = _many("ui", [
    T("сделай английский интерфейс", "ru", "lang:en"),
    T("на кириллицу переключи", "ru", "lang:uz-Cyrl"),
    T("Ingliz tiliga oʻtkazing", "uz", "lang:en"),
    T("Yorugʻ rejimga qayting", "uz", "theme:light"),
    T("Инглиз тилига ўтказинг", "uz-Cyrl", "lang:en"),
    T("Қоронғи фон қилинг", "uz-Cyrl", "theme:dark"),
    T("Switch to Russian", "en", "lang:ru"),
    T("Switch the language to Uzbek", "en", "lang:uz"),
    T("Dark mode please", "en", "theme:dark"),
    T("Light theme", "en", "theme:light"),
])

NONE = _many("none", [
    T("Удали всё", "ru", "none"),
    T("Добавь сотрудника Алишера", "ru", "none"),
    T("Hi", "en", "none"),
    T("Thanks!", "en", "none"),
    T("What's the weather today?", "en", "none"),
    T("Delete all products", "en", "none"),
    T("Change admin password to 1234", "en", "none"),
    T("Make a backup now", "en", "none"),
    T("Delete all employees", "en", "none"),
    T("change the price", "en", "none"),
    T("Set kefir price to 9000", "en", "none"),
    T("Rahmat", "uz", "none"),
    T("Hamma tovarlarni oʻchiring", "uz", "none"),
    T("Parolni oʻzgartiring", "uz", "none"),
    T("Kefir narxini 9000 qiling", "uz", "none"),
    T("Oʻchiring", "uz", "none"),
    T("Салом", "uz-Cyrl", "none"),
    T("Раҳмат", "uz-Cyrl", "none"),
    T("Кефир нархини 9000 қилинг", "uz-Cyrl", "none"),
])


def _d(id_: str, *turns, apply: bool = False) -> dict:
    d = {"id": id_, "catalog": "small", "turns": list(turns)}
    if apply:
        d["apply"] = True
    return d


FOLLOW = [
    _d("follow_ru_1", T("Поставь цену пепси 15000", "ru", "upd:PEPSI:price=15000"),
       T("нет, не пепси, а фанте", "ru", "upd:FANTA:price=15000")),
    _d("follow_ru_2", T("Поставь цену фанты 9000", "ru", "upd:FANTA:price=9000", db={"FANTA": {"price": 9000}}),
       T("Сколько теперь стоит фанта?", "ru", _FIND, ["9000"]), apply=True),
    _d("follow_ru_3", T("Убери пепси в архив", "ru", "status:PEPSI:archived", db={"PEPSI": {"status": "archived"}}),
       T("Сколько товаров в архиве?", "ru", _COUNT, ["2"]), apply=True),
    _d("follow_ru_4", T("Сколько стоит сникерс?", "ru", _FIND, ["7000"]),
       T("переименуй его в Snickers 50 г шоколад", "ru", "upd:SNICK:name")),
    _d("follow_uz_1", T("Pepsini arxivga oling", "uz", "status:PEPSI:archived"),
       T("yoʻq, Pepsi emas, Fantani", "uz", "status:FANTA:archived")),
    _d("follow_uz_2", T("Coca-Cola qancha?", "uz", _FIND, ["9000"]),
       T("narxini 500 ga oshiring", "uz", "upd:COLA:price=9500")),
    _d("follow_uz_3", T("Barcha ichimliklar narxini 10% oshiring", "uz", "bulk:price_percent:category=Напитки",
                        db={"COLA": {"price": 9900}}),
       T("Coca-Cola endi qancha turadi?", "uz", _FIND, ["9900"]), apply=True),
    _d("follow_uzc_1", T("Пепси нархини 15000 қилинг", "uz-Cyrl", "upd:PEPSI:price=15000"),
       T("йўқ, Пепси эмас, Фантани", "uz-Cyrl", "upd:FANTA:price=15000")),
    _d("follow_uzc_2", T("Нутелла борми?", "uz-Cyrl", _FIND, ["nutella|нутелла"]),
       T("уни архивга олинг", "uz-Cyrl", "status:NUT:archived")),
    _d("follow_en_1", T("Archive Pepsi", "en", "status:PEPSI:archived"),
       T("no, not Pepsi, Fanta", "en", "status:FANTA:archived")),
    _d("follow_en_2", T("How much is Pepsi?", "en", _FIND, ["14000"]),
       T("and Fanta?", "en", _FIND, ["8500"])),
    _d("follow_en_3", T("Do we have Nutella?", "en", _FIND, ["nutella"]),
       T("set its price to 50000", "en", "upd:NUT:price=50000")),
    _d("follow_en_4", T("Raise prices of all drinks by 10%", "en", "bulk:price_percent:category=Напитки",
                        db={"COLA": {"price": 9900}}),
       T("How much is Coca-Cola now?", "en", _FIND, ["9900"]), apply=True),
]

DIALOGS: list[dict] = FIND + COUNT + MISSING + OTHER_READ + ADD + UPDATE + ARCHIVE + COPY + BULK + LIST + UI + NONE + FOLLOW
