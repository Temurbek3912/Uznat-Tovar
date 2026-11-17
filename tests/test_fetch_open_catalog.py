"""scripts/fetch_open_catalog.py — чистые функции: выбор фото, очистка названий и брендов, папки фото. Без сети."""
from scripts import fetch_open_catalog as foc


def test_photo_ids_front_first_then_uploads_without_repeats():
    images = {"1": {}, "2": {}, "3": {}, "front_ru": {"imgid": "3"}, "nutrition_fr": {"imgid": "2"}}
    assert foc.photo_ids(images) == ["3", "1", "2"]
    assert foc.photo_ids({str(i): {} for i in range(1, 9)}) == ["1", "2", "3", "4", "5"]
    assert foc.photo_ids({}) == []
    assert foc.photo_ids({"front_en": {"imgid": "7"}}) == []          # выбранное фото без самой загрузки — не берём


def test_name_prefers_russian_unescapes_html_and_adds_quantity():
    p = {"product_name": "Bonbons", "product_name_ru": "Конфеты &quot;Рошен&quot;", "quantity": "1 kg"}
    assert foc.product_name(p) == 'Конфеты "Рошен" 1 kg'
    assert foc.product_name({"product_name": "Pepsi 1.5", "quantity": "1.5"}) == "Pepsi 1.5"     # объём уже в названии
    assert foc.product_name({"product_name": "3913"}) == ""                                          # мусор — не название
    assert foc.product_name({"product_name_uz": "Suv", "product_name": "1"}) == "Suv"


def test_brand_first_of_list_and_junk_dropped():
    assert foc.brand_of({"brands": "Roshen, Konti"}) == "Roshen"
    assert foc.brand_of({"brands": ["Nestle"]}) == "Nestle"
    assert foc.brand_of({"brands": "3013"}) == ""


def test_image_dir_splits_long_codes():
    assert foc._image_dir("4780069000147") == "478/006/900/0147"
    assert foc._image_dir("76000010") == "76000010"


def test_rich_takes_products_with_many_photos_most_first_up_to_15():
    many = {"code": "1", "product_name": "Pepsi", "quantity": "1,5 л", "images": {str(i): {} for i in range(1, 31)}}
    few = {"code": "2", "product_name": "Fanta", "images": {"1": {}, "2": {}}}
    mid = {"code": "3", "product_name": "Sprite", "images": {str(i): {} for i in range(1, 8)}}
    assert [p["code"] for p in foc.pick_rich([few, mid, many], 6, 10)] == ["1", "3"]
    assert len(foc.photo_ids(many["images"], foc.RICH_MAX_PHOTOS)) == 15
    assert len(foc.photo_ids(many["images"])) == 5                    # обычный режим — как раньше


def test_rich_queries_and_alcohol_category():
    q = foc.rich_queries(["pepsi"], ["uzbekistan"], alcohol=True)
    assert q[0] == 'brands_tags:"pepsi" AND countries_tags:"en:uzbekistan"'
    assert any("en:beers" in x for x in q)
    assert foc.is_alcohol_tags(["en:beverages", "en:alcoholic-beverages", "en:beers"])
    assert not foc.is_alcohol_tags(["en:beers", "en:non-alcoholic-beverages"])
    assert foc.category_of({"categories_tags": ["en:beers", "en:alcoholic-beverages"]}) == "Алкоголь (21+)"
    assert foc.RU_KINDS["tobacco"] == "Табак (21+)"
