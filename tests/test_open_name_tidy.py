"""app/services/open_name_tidy.py — чистка названий из открытой базы при загрузке. Примеры — из ручной чистки 2026-11-18."""
import csv

from app.services.open_name_tidy import capitalize_first, name_doubts, tidy_brand, tidy_name
from scripts import fetch_open_catalog as foc
from scripts import import_catalog
from app.services.catalog_import import ProductMeta


def test_ingredients_calories_price_arabic_and_marks_are_removed():
    assert tidy_name("Ebish Original strawberry 🍓 Beverage Ingredients: Aqua, Sugar") == "Ebish Original strawberry Beverage"
    assert tidy_name("Читос сыр 597 кдж 141ккал жиры 6,6г нжк 0,6г сахар 1,1г соль 0,6г") == "Читос сыр"
    assert tidy_name("Сок Состав: яблоко, вода") == "Сок"
    assert tidy_name("НАФТОЗАЛИН, 3.500 СУМ, 1 БУТЫЛКА") == "НАФТОЗАЛИН, 1 БУТЫЛКА"
    assert tidy_name("Кола 12000 so'm") == "Кола"
    assert tidy_name("Ringo 10جرام 14جرام") == "Ringo"
    assert tidy_name("Chupa chups® роллсы") == "Chupa chups роллсы"
    assert tidy_name("Молоко 2,5℅ 500 г") == "Молоко 2,5% 500 г"
    assert tidy_name("Нагетсы куриные масса нетто: 300 г") == "Нагетсы куриные 300 г"
    assert tidy_name("Saucisson sec 250g.") == "Saucisson sec 250g"
    assert tidy_name("Био-С Имун+. Персик.") == "Био-С Имун+. Персик"
    assert tidy_name("Мини-рулеты клубничные 5 шт.") == "Мини-рулеты клубничные 5 шт."   # точка сокращения остаётся
    assert tidy_brand("Josef Manner & comp.") == "Josef Manner & comp."


def test_first_letter_capital_but_size_and_brands_like_iphone_kept():
    assert tidy_name("ветчина 500 г") == "Ветчина 500 г"
    assert tidy_name("nescafe gold") == "Nescafe gold"
    assert capitalize_first("iPhone 15") == "iPhone 15"
    assert capitalize_first("«сластёна» 120 г") == "«Сластёна» 120 г"
    assert tidy_name("Pepsi 1.5") == "Pepsi 1.5"                     # объём и цифры без «сум» не трогаем
    assert tidy_name("Сок 1 л 2000 мл") == "Сок 1 л 2000 мл"


def test_nothing_left_gives_empty_and_brand_keeps_case():
    assert tidy_name("جرام 10") == "10"
    assert tidy_name("") == ""
    assert tidy_brand("شكل جديد") == ""
    assert tidy_brand("adidas®") == "adidas"


def test_doubts_point_at_what_a_human_must_fix():
    assert "в слове смешаны русские и латинские буквы — опечатка?" in name_doubts("Дошuрак")
    assert "похоже на набор букв" in name_doubts("GCJEIVKDIFHFURUGHDH JCJFIBMBJFKVJ")
    assert any("длинное" in w for w in name_doubts("Мороженое сливочное с вареным сгущеным молоком и грецким орехом "
                                                   "в глазированном сахарном вафельном рожке"))
    assert name_doubts("Nestle", "Nestle") == ["в названии только бренд — какой товар?"]
    assert name_doubts("Water", "Не указан") == ["одно слово и нет бренда — какой товар?"]
    assert name_doubts("Ветчина 500 г", "Черкизово") == []
    assert name_doubts("Кетчуп", "Heinz") == []                         # одно слово, но с брендом — нормально


def test_last_manual_cleanup_list_capital_letter_rows_now_happen_by_themselves():
    rows = list(csv.DictReader(open("docs/catalog_cleanup/names_2026-11-18.csv", encoding="utf-8")))
    caps = [r for r in rows if r["why"] == "первая буква маленькая"]
    assert len(caps) > 30
    assert all(tidy_name(r["old_name"]) == r["new_name"] for r in caps)


def test_fetch_uses_tidy_and_writes_names_to_check(tmp_path):
    assert foc.product_name({"product_name": "ветчина Ingredients: pork", "quantity": "500 г"}) == "Ветчина 500 г"
    assert foc.brand_of({"brands": "Nestle®"}) == "Nestle"
    rows = [{"sku": "UZ-1", "name": "Дошuрак", "brand": "", "barcode": "1"},
            {"sku": "UZ-2", "name": "Ветчина 500 г", "brand": "Черкизово", "barcode": "2"}]
    assert foc.write_names_to_check(tmp_path, rows) == 1
    got = list(csv.DictReader(open(tmp_path / "names_to_check.csv", encoding="utf-8")))
    assert [(r["sku"], r["action"], r["old_name"]) for r in got] == [("UZ-1", "rename", "Дошuрак")]
    assert foc.write_names_to_check(tmp_path, rows[1:]) == 0
    assert not (tmp_path / "names_to_check.csv").exists()               # старый список не остаётся


def test_import_tidies_names_and_warns_about_doubtful_ones():
    metas = {"A": ProductMeta("A", "ветчина 300 сум"), "B": ProductMeta("B", "Water"), "C": ProductMeta("C", "جرام")}
    fixed, notes = import_catalog.tidy_names(metas)
    assert fixed["A"].name == "Ветчина"
    assert fixed["C"].name == "جرام"                                       # ничего не осталось — оставляем как было
    assert any("A: название поправлено" in n for n in notes)
    assert any("B: проверить название «Water»" in n for n in notes)
