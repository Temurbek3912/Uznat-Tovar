"""
Вид товара — «это напиток», «это обувь» (решение владельца 2026-11-14: любой товар хотя бы по виду; документ 07, §10.7).

Один закрытый список видов на всю программу. Сервер отдаёт только код вида (`drink`, `footwear`, …), подпись на языке
покупателя — в словарях страниц (`kind.<код>` в frontend/i18n.*.js). Список общий для:
  * большой модели «зрение + текст» (LM Studio) — код вида в ответе JSON, enum из KIND_IDS;
  * малой быстрой модели (app/pipeline/kind_classifier.py) — сравнение кадра с описаниями PROMPTS (только английский: так
    модели обучены);
  * замера ml/benchmark_kinds.py — соответствие категорий открытых баз видам (OFF_CATEGORY_KINDS).

Порядок видов — от частых в продуктовом магазине к редким; первый подходящий вид выигрывает при сопоставлении категорий.
"""
from __future__ import annotations

# код -> описания для малой модели (несколько описаний на вид: модель сравнивает кадр с каждым, берётся лучшее)
PROMPTS: dict[str, tuple[str, ...]] = {
    "drink": ("a bottle of water", "a bottle of soda", "a can of soft drink", "a carton of juice", "a bottle of beer",
              "a can of energy drink", "a bottle of wine", "a plastic bottle of a drink"),
    "dairy": ("a carton of milk", "a cup of yogurt", "a pack of cheese", "a pack of butter", "a jar of sour cream",
              "a bottle of kefir"),
    "sweets": ("a chocolate bar", "a box of chocolates", "a pack of candy", "a pack of cookies", "a pack of biscuits",
               "a pack of chewing gum", "a cake or wafer in a wrapper"),
    "snack": ("a bag of potato chips", "a can of crisps", "a bag of crackers", "a pack of nuts", "a bag of popcorn",
              "a pack of sunflower seeds"),
    "tea_coffee": ("a box of tea", "a pack of tea bags", "a jar of instant coffee", "a pack of ground coffee",
                   "a pack of coffee capsules"),
    "grocery": ("a pack of pasta", "a bag of rice", "a bag of flour", "a pack of cereal", "a bottle of cooking oil",
                "a bag of sugar", "a pack of buckwheat", "a box of breakfast cereal"),
    "sauce": ("a bottle of ketchup", "a jar of mayonnaise", "a jar of mustard", "a pack of spices", "a bottle of sauce",
              "a jar of jam or honey", "a pack of salt"),
    "canned": ("a can of food", "a tin of fish", "a can of beans", "a glass jar of pickles", "a can of meat"),
    "bakery": ("a loaf of bread", "a pack of buns", "a packaged pastry", "a pack of flatbread", "a bag of sliced bread",
               "a packaged loaf of sandwich bread", "a bag of brioche"),
    "meat_fish": ("a pack of sausages", "a stick of salami", "a pack of raw meat", "a pack of fish", "a pack of chicken"),
    "fruit_veg": ("a fresh fruit", "fresh vegetables", "an apple", "a banana", "tomatoes", "a bunch of herbs",
                  "a box of fresh berries", "a net bag of oranges or lemons", "a bunch of bananas", "a punnet of grapes"),
    "frozen": ("a pack of frozen dumplings", "a box of ice cream", "a bag of frozen vegetables", "a frozen pizza"),
    "baby": ("a jar of baby food", "a pack of baby formula", "a pack of diapers", "a baby bottle", "a box of baby cereal",
             "a can of infant formula milk", "a pouch of baby puree"),
    "medicine": ("a box of medicine pills", "a blister pack of tablets", "a bottle of medicine syrup",
                 "a pharmacy medicine box", "a tube of medical ointment"),
    "cosmetics": ("a bottle of shampoo", "a tube of toothpaste", "a bar of soap", "a jar of face cream", "a deodorant",
                  "a lipstick or makeup", "a bottle of perfume", "a pack of razors", "a pack of toilet paper"),
    "cleaning": ("a bottle of dishwashing liquid", "a pack of laundry detergent", "a bottle of cleaning spray",
                 "a bottle of bleach", "a pack of sponges"),
    "home": ("a cooking pot or pan", "a plate or cup", "a kitchen utensil", "a light bulb", "a pack of batteries",
             "a plastic container", "a towel", "a household tool",
             "a fly swatter", "a broom or mop", "a bucket or basin", "a hanger or clothes pegs"),
    "clothing": ("a t-shirt", "a pair of jeans", "a jacket", "a dress", "a pair of socks", "a hat", "clothes on a hanger"),
    "bags": ("a backpack", "a handbag", "a suitcase", "a school bag", "a sports bag", "a shopping bag made of fabric"),
    "accessories": ("a wristwatch", "a pair of glasses", "a pair of sunglasses", "a necklace or bracelet", "a ring or earrings",
                    "a leather belt", "a wallet", "an umbrella"),
    "footwear": ("a pair of shoes", "a pair of sneakers", "a pair of boots", "a pair of sandals", "a pair of slippers"),
    "electronics": ("a smartphone", "a pair of headphones", "a phone charger", "a laptop", "a computer mouse",
                    "a USB cable", "a small electronic device in a box"),
    "stationery": ("a notebook", "a pack of pens", "a pencil", "a school exercise book", "office supplies"),
    "toy": ("a toy", "a plush toy", "a toy car", "a doll", "a box of a board game", "building blocks"),
    "pet": ("a bag of dog food", "a bag of cat food", "a can of pet food", "a pet accessory"),
    "tobacco": ("a pack of cigarettes", "an electronic cigarette", "a lighter"),
}

KIND_IDS: tuple[str, ...] = tuple(PROMPTS)

# «не товар»: малая модель выбирает между видами и этими описаниями; ближе к ним — вид не называется
NOT_PRODUCT_PROMPTS: tuple[str, ...] = (
    "a blurry photo", "a photo of a floor", "a photo of a wall", "a photo of a person", "a photo of a hand",
    "an empty shelf", "a dark photo", "a photo of a room", "random noise", "a plain colored surface",
)

# для большой модели — что значит каждый код (в подсказке к JSON)
VISION_HINTS: dict[str, str] = {
    "drink": "water, soda, juice, energy drinks, iced tea in bottles, beer, wine",
    "dairy": "milk, yogurt, cheese, butter, kefir, cream",
    "sweets": "chocolate, candy, cookies, biscuits, wafers, cakes, chewing gum",
    "snack": "chips, crisps, crackers, nuts, seeds, popcorn",
    "tea_coffee": "tea leaves or tea bags, coffee beans, ground or instant coffee",
    "grocery": "pasta, noodles, rice, flour, grains, breakfast cereals, cooking oil, sugar, gnocchi, ready-to-cook meals",
    "sauce": "ketchup, mayonnaise, mustard, sauces, spices, salt, jam, honey",
    "canned": "food in tins or jars: canned fish, beans, vegetables, meat",
    "bakery": "bread, sliced bread, buns, brioche, pastry",
    "meat_fish": "fresh or chilled sausages, ham, meat, chicken, fish (not in tins)",
    "fruit_veg": "fresh fruit, berries and vegetables",
    "frozen": "ice cream of any kind and frozen food (dumplings, frozen vegetables, frozen pizza)",
    "baby": "baby food, baby cereal, infant formula, diapers",
    "medicine": "medicines: pills, tablets, syrups, ointments, pharmacy products",
    "cosmetics": "for the body: shampoo, soap, shower gel, toothpaste, creams, deodorant, makeup, perfume, toilet paper",
    "cleaning": "for the house, not the body: laundry detergent, dishwashing liquid, cleaners, bleach",
    "home": "household and kitchen goods: batteries, light bulbs, dishes, pots, tools, containers, brooms, fly swatters",
    "clothing": "clothes: shirts, trousers, dresses, jackets, socks, underwear, hats",
    "bags": "bags of any kind: backpacks, handbags, suitcases, school bags, sports bags",
    "accessories": "personal accessories: watches, glasses and sunglasses, jewelry, belts, wallets, umbrellas",
    "footwear": "any shoes: sneakers, boots, sandals, slippers (always footwear, never clothing)",
    "electronics": "phones, chargers, headphones, computers, gadgets (batteries are home)",
    "stationery": "pens, pencils, markers, notebooks, office supplies",
    "toy": "toys, plush toys, dolls, board games, building sets",
    "pet": "anything for cats, dogs or other animals: pet food, cat treats, dog biscuits, pet accessories",
    "tobacco": "cigarettes, tobacco, rolling papers, lighters",
}

# категории открытых баз (Open Food/Beauty/Products/Pet Food Facts) -> вид; для замера. Проверяются по порядку.
OFF_CATEGORY_KINDS: tuple[tuple[str, str], ...] = (
    ("en:baby-foods", "baby"), ("en:baby-milks", "baby"), ("en:diapers", "baby"),
    ("en:ice-creams", "frozen"), ("en:frozen-foods", "frozen"),
    ("en:teas", "tea_coffee"), ("en:coffees", "tea_coffee"),
    ("en:dairies", "dairy"), ("en:cheeses", "dairy"), ("en:yogurts", "dairy"), ("en:milks", "dairy"),
    ("en:beverages", "drink"),
    ("en:chocolates", "sweets"), ("en:confectioneries", "sweets"), ("en:biscuits-and-cakes", "sweets"),
    ("en:candies", "sweets"),
    ("en:chips-and-fries", "snack"), ("en:salty-snacks", "snack"), ("en:nuts", "snack"), ("en:crackers", "snack"),
    ("en:sauces", "sauce"), ("en:condiments", "sauce"), ("en:spreads", "sauce"), ("en:honeys", "sauce"),
    ("en:canned-foods", "canned"),
    ("en:breads", "bakery"),
    ("en:meats", "meat_fish"), ("en:seafood", "meat_fish"), ("en:fishes", "meat_fish"),
    ("en:fresh-fruits", "fruit_veg"), ("en:fresh-vegetables", "fruit_veg"),
    ("en:pastas", "grocery"), ("en:rices", "grocery"), ("en:flours", "grocery"), ("en:breakfast-cereals", "grocery"),
    ("en:vegetable-oils", "grocery"), ("en:sugars", "grocery"), ("en:cereals-and-potatoes", "grocery"),
)


def is_kind(value) -> bool:
    return isinstance(value, str) and value in PROMPTS


def kind_for_categories(tags: list[str]) -> str | None:
    """Категории товара из открытой базы -> вид (первое совпадение по порядку OFF_CATEGORY_KINDS) или None."""
    have = set(tags or [])
    for tag, kind in OFF_CATEGORY_KINDS:
        if tag in have:
            return kind
    return None
