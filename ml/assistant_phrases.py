"""
Проверочные фразы ИИ-помощника (ml/benchmark_assistant.py, замер 2026-11-23). Каталог замера — CATALOG ниже (временная база).

Ожидание — строка, несколько вариантов через «|»:
  read:<инструмент>             — смотрит данные этим инструментом, ничего не предлагает;
  upd:<артикул>:<поле>          — карточка «изменить поле товара»;
  status:<артикул>:<archived|active> — карточка «в архив / из архива»;
  add                           — карточка «новый товар»;
  bulk:<change>:<where>         — карточка «одно изменение многим товарам»;
  lang:<язык>, theme:<тема>     — сменить язык / фон сразу;
  none                          — ничего не менять (вопрос не о программе, неясная просьба — уточнить, а не угадывать).
Язык страницы (ru / uz / uz-Cyrl) — второе поле.
"""
from __future__ import annotations

CATALOG = [
    # артикул, название, бренд, категория, цена, штрихкод
    ("COLA", "Кока-кола 0,5 л", "Coca-Cola", "Напитки", 9000, "4780001234562"),
    ("FANTA", "Fanta апельсин 0,5 л", "Coca-Cola", "Напитки", 8500, "4780001234579"),
    ("PEPSI", "Pepsi 1,5 л", "Pepsi", "Напитки", 14000, None),
    ("WATER", "Вода Hydrolife 1,5 л", "Hydrolife", "Напитки", 4000, None),
    ("NUT", "Nutella 400 г", "Ferrero", "Сладости", 45000, "8000500310427"),
    ("SNICK", "Snickers 50 г", "Mars", "Сладости", 7000, None),
    ("PRING", "Pringles Original 165 г", "Pringles", "Снеки", 32000, None),
    ("LAYS", "Lay's сметана и зелень 90 г", "Lay's", "Снеки", 12000, None),
    ("BREAD", "Хлеб белый", "Не указан", "Хлеб", 4000, None),
    ("MILK", "Молоко Nestle 1 л", "Nestle", "Молочные", None, None),
]
ARCHIVED = [("JUICE", "Сок Добрый яблоко 1 л", "Добрый", "Напитки", 15000, None)]

PHRASES: list[tuple[str, str, str]] = [
    # --- сводка каталога
    ("Сколько у меня товаров?", "ru", "read:catalog_overview"),
    ("скока товаров в базе", "ru", "read:catalog_overview"),
    ("Какие у нас категории?", "ru", "read:catalog_overview"),
    ("Nechta tovar bor?", "uz", "read:catalog_overview"),
    ("Katalogda qancha mahsulot bor", "uz", "read:catalog_overview"),
    ("Нечта товар бор?", "uz-Cyrl", "read:catalog_overview"),
    ("Каталогда қанча маҳсулот бор?", "uz-Cyrl", "read:catalog_overview"),
    # --- найти товар / цена
    ("Есть у нас пепси?", "ru", "read:find_products"),
    ("Сколько стоит нутелла?", "ru", "read:find_products"),
    ("цена сникерса", "ru", "read:find_products"),
    ("найди чипсы принглс", "ru", "read:find_products"),
    ("Pepsi bormi?", "uz", "read:find_products"),
    ("Nutella qancha turadi?", "uz", "read:find_products"),
    ("Fanta narxi qancha", "uz", "read:find_products"),
    ("Нутелла қанча туради?", "uz-Cyrl", "read:find_products"),
    ("Пепси борми?", "uz-Cyrl", "read:find_products"),
    ("кока кола скока стоит", "ru", "read:find_products"),
    ("найди товар 4780001234562", "ru", "read:find_products|read:product_details"),
    ("Что за товар со штрихкодом 8000500310427?", "ru", "read:find_products|read:product_details"),
    ("Расскажи всё про товар PRING", "ru", "read:product_details|read:find_products"),
    ("PRING haqida hammasini ayt", "uz", "read:product_details|read:find_products"),
    # --- чего нет у товаров
    ("Какие товары без фото?", "ru", "read:products_missing"),
    ("у каких товаров нет штрихкода", "ru", "read:products_missing"),
    ("Товары без цены покажи", "ru", "read:products_missing"),
    ("где не указан бренд", "ru", "read:products_missing"),
    ("Rasmsiz tovarlar qaysilar?", "uz", "read:products_missing"),
    ("Shtrixkodi yoʻq tovarlar", "uz", "read:products_missing"),
    ("Narxi yoʻq mahsulotlar qaysi?", "uz", "read:products_missing"),
    ("Расмсиз товарлар қайсилар?", "uz-Cyrl", "read:products_missing"),
    ("Нархи йўқ маҳсулотлар", "uz-Cyrl", "read:products_missing"),
    # --- дубли
    ("Есть ли дубли товаров?", "ru", "read:similar_names"),
    ("одинаковые названия есть?", "ru", "read:similar_names"),
    ("Bir xil nomli tovarlar bormi?", "uz", "read:similar_names"),
    ("Бир хил номли товарлар борми?", "uz-Cyrl", "read:similar_names"),
    # --- почему не узнаёт
    ("Почему программа не узнаёт товары?", "ru", "read:scan_problems"),
    ("что плохо распознаётся за неделю", "ru", "read:scan_problems"),
    ("Nega dastur tovarlarni tanimayapti?", "uz", "read:scan_problems"),
    ("Нега дастур товарларни танимаяпти?", "uz-Cyrl", "read:scan_problems"),
    # --- сотрудники, копии
    ("Кто у меня работает?", "ru", "read:staff_list"),
    ("список сотрудников", "ru", "read:staff_list"),
    ("Xodimlar roʻyxati", "uz", "read:staff_list"),
    ("Ходимлар рўйхатини кўрсат", "uz-Cyrl", "read:staff_list"),
    ("Когда была последняя резервная копия?", "ru", "read:backup_status"),
    ("бэкап давно делали?", "ru", "read:backup_status"),
    ("Zaxira nusxa qachon olingan?", "uz", "read:backup_status"),
    ("Заҳира нусха қачон олинган?", "uz-Cyrl", "read:backup_status"),
    # --- изменить одно поле
    ("Поставь цену фанты 9000", "ru", "upd:FANTA:price"),
    ("Сделай нутеллу 47000", "ru", "upd:NUT:price"),
    ("цену пепси поменяй на 15 тысяч", "ru", "upd:PEPSI:price"),
    ("Fanta narxini 9000 qiling", "uz", "upd:FANTA:price"),
    ("Pepsi narxini 15000 ga oʻzgartiring", "uz", "upd:PEPSI:price"),
    ("Фанта нархини 9000 қилинг", "uz-Cyrl", "upd:FANTA:price"),
    ("Молоку поставь цену 12000", "ru", "upd:MILK:price"),
    ("Переименуй хлеб белый в Хлеб пшеничный", "ru", "upd:BREAD:name"),
    ("Сникерсу поставь бренд Mars Inc", "ru", "upd:SNICK:brand"),
    ("Поставь Pringles бренд Lays", "ru", "upd:PRING:brand"),
    ("Pringles brendini Lays qiling", "uz", "upd:PRING:brand"),
    ("Переведи воду Hydrolife в категорию Вода", "ru", "upd:WATER:category"),
    ("Fanta kategoriyasini Gazli ichimliklar qiling", "uz", "upd:FANTA:category"),
    ("Пепси закончилась", "ru", "upd:PEPSI:in_stock"),
    ("Snickers tugadi", "uz", "upd:SNICK:in_stock"),
    ("Добавь кока-коле штрихкод 4780001234999", "ru", "upd:COLA:add_barcode"),
    # --- архив
    ("Убери пепси в архив", "ru", "status:PEPSI:archived"),
    ("удали сникерс", "ru", "status:SNICK:archived"),
    ("Хлеб больше не продаём, убери", "ru", "status:BREAD:archived"),
    ("Pepsini arxivga oling", "uz", "status:PEPSI:archived"),
    ("Snickersni oʻchiring", "uz", "status:SNICK:archived"),
    ("Пепсини архивга олинг", "uz-Cyrl", "status:PEPSI:archived"),
    ("Верни сок Добрый из архива", "ru", "status:JUICE:active"),
    ("Dobriy sharbatini arxivdan qaytaring", "uz", "status:JUICE:active"),
    # --- новый товар
    ("Добавь новый товар Спрайт 0,5 л за 8500", "ru", "add"),
    ("Новый товар: Mars 50 г, цена 7000", "ru", "add"),
    ("Yangi tovar qoʻshing: Sprite 0,5 l, narxi 8500", "uz", "add"),
    ("Янги товар қўшинг: Спрайт 0,5 л, нархи 8500", "uz-Cyrl", "add"),
    # --- много товаров сразу
    ("Подними цены на все напитки на 10%", "ru", "bulk:price_percent:category"),
    ("Все снеки убери в архив", "ru", "bulk:archive:category"),
    ("Сделай скидку 5% на всё от Coca-Cola", "ru", "bulk:price_percent:brand"),
    ("Barcha ichimliklar narxini 10% oshiring", "uz", "bulk:price_percent:category"),
    ("Barcha gazaklarni arxivga oling", "uz", "bulk:archive:category"),
    ("Барча ичимликлар нархини 10% оширинг", "uz-Cyrl", "bulk:price_percent:category"),
    ("Все сладости добавь 1000 к цене", "ru", "bulk:price_add:category"),
    # --- язык и фон
    ("Переключи на узбекский", "ru", "lang:uz"),
    ("Сделай язык английский", "ru", "lang:en"),
    ("Tilni rus tiliga oʻzgartiring", "uz", "lang:ru"),
    ("Кириллицага ўтказинг", "uz-Cyrl", "lang:uz-Cyrl"),
    ("Тилни лотинчага ўтказинг", "uz-Cyrl", "lang:uz"),
    ("Сделай тёмный фон", "ru", "theme:dark"),
    ("ночной режим включи", "ru", "theme:dark"),
    ("Светлую тему верни", "ru", "theme:light"),
    ("Qorongʻi rejim", "uz", "theme:dark"),
    ("Ёруғ фон қилинг", "uz-Cyrl", "theme:light"),
    # --- похожие, но другие команды (путаница)
    ("Сколько стоит пепси и поменяй её цену на 15000", "ru", "upd:PEPSI:price"),
    ("Какая цена у фанты? Не меняй ничего", "ru", "read:find_products"),
    ("Покажи товары без категории", "ru", "read:products_missing"),
    ("Сколько товаров в категории напитки?", "ru", "read:catalog_overview|read:find_products"),
    ("Удалить фото у хлеба", "ru", "none"),
    ("Верни цену фанты как было", "ru", "none"),
    ("Pepsi narxi qancha? Oʻzgartirmang", "uz", "read:find_products"),
    ("Сникерс есть в наличии?", "ru", "read:find_products"),
    ("Snickers bormi doʻkonda?", "uz", "read:find_products"),
    # --- неясно или не о программе: не угадывать, не менять
    ("удали", "ru", "none"),
    ("поменяй цену", "ru", "none"),
    ("сделай дешевле", "ru", "none"),
    ("Narxini oʻzgartiring", "uz", "none"),
    ("Какая сегодня погода?", "ru", "none"),
    ("Расскажи анекдот", "ru", "none"),
    ("Bugun ob-havo qanday?", "uz", "none"),
    ("Привет", "ru", "none"),
    ("Salom", "uz", "none"),
    ("Спасибо!", "ru", "none"),
    ("Удали всех сотрудников", "ru", "none"),
    ("Поменяй пароль администратора на 1234", "ru", "none"),
    ("Сделай резервную копию", "ru", "none"),
    ("Удали все товары", "ru", "none"),
    ("Поставь всем товарам цену 0", "ru", "none"),
    ("Барча товарларни ўчиринг", "uz-Cyrl", "none"),
    ("Удали товар Спрайт", "ru", "none"),                  # такого товара нет — карточки быть не должно
    ("Поставь цену кефиру 9000", "ru", "none"),            # кефира нет
    # --- с опечатками и разговорные
    ("скок стоит нутела", "ru", "read:find_products"),
    ("пасмотри товары без фотки", "ru", "read:products_missing"),
    ("фанту на 9 тыщ поставь", "ru", "upd:FANTA:price"),
    ("пепсю в архив", "ru", "status:PEPSI:archived"),
    ("темную тему врубай", "ru", "theme:dark"),
    ("nutela qancha", "uz", "read:find_products"),
    ("pepsini arxivga", "uz", "status:PEPSI:archived"),
    ("fantani narxi 9000 bolsin", "uz", "upd:FANTA:price"),
]
