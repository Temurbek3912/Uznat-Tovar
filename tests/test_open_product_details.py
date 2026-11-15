"""app/services/open_product_details.py и выбор/дополнение в scripts (enrich_open_catalog, fetch_open_catalog.pick_varied). Без сети и базы."""
from types import SimpleNamespace

from app.services import open_product_details as opd
from scripts import enrich_open_catalog as enrich
from scripts import fetch_open_catalog as foc


def test_country_by_gs1_prefix():
    assert opd.country_from_barcode("4780069000147") == "Узбекистан"
    assert opd.country_from_barcode("4600080431112") == "Россия"
    assert opd.country_from_barcode("8695077046994") == "Турция"
    assert opd.country_from_barcode("4870001234567") == "Казахстан"
    assert opd.country_from_barcode("045860307036") == "США"             # UPC-A без нуля
    assert opd.country_from_barcode("2000000000015") == ""                # внутренний код магазина
    assert opd.country_from_barcode("76000010") == ""                     # EAN-8 — не угадываем
    assert opd.country_from_barcode("") == ""


def test_net_content_units_to_russian_and_from_name():
    assert opd.net_content("1 L") == "1 л"
    assert opd.net_content("500ml") == "500 мл"
    assert opd.net_content("0.33 l") == "0,33 л"
    assert opd.net_content("1kg") == "1 кг"
    assert opd.net_content("", "Fizzy Boom! 1kg") == "1 кг"
    assert opd.net_content("", "Coca-Cola 0,33 л") == "0,33 л"
    assert opd.net_content("", "Печенье Oreo Original") == ""
    assert opd.amount_in_base("500 г") == 0.5 and opd.amount_in_base("330 мл") == 0.33 and opd.amount_in_base("10 шт") is None


def test_ingredients_only_when_they_look_like_ingredients():
    junk = "6 6 (100 ml): kal/176 kJ 9 ig 9 HARORATDA, QURUQ VA DA ISTE'MOL QILING."
    assert opd.ingredients_of({"ingredients_text": junk}) == ""
    assert opd.ingredients_of({"ingredients_text_ru": "Состав: вода, сахар, _молоко_"}) == "Вода, сахар, молоко"
    assert opd.ingredients_of({"ingredients_text_ru": "", "ingredients_text_uz": "suv, shakar"}) == "Suv, shakar"
    assert opd.ingredients_of({"ingredients_text_ru": "Энергетическая ценность 250 ккал, белки"}) == ""
    long = ", ".join(["сахар"] * 400)
    assert len(opd.ingredients_of({"ingredients_text_ru": long})) <= opd.MAX_INGREDIENTS + 1


def test_category_from_name_and_demo_price():
    assert opd.category_from_name("Coca-Cola 0,5 л") == "Напитки"
    assert opd.category_from_name("Шоколад Alpen Gold") == "Сладости"
    assert opd.category_from_name("Шампунь Head & Shoulders") == "Косметика и гигиена"
    assert opd.category_from_name("Qwerty") == ""
    p1 = opd.demo_price("Напитки", "1 л", "4780069000147")
    assert 7_000 <= p1 <= 11_000 and p1 % 500 == 0
    assert opd.demo_price("Напитки", "1 л", "a") == opd.demo_price("Напитки", "1 л", "a")      # одинаково при повторе
    assert opd.demo_price("Сладости", "10 г", "x") >= 2_500                                   # не меньше нижней границы
    assert opd.demo_price("Неизвестно", "", "x") > 0


def test_plan_changes_fills_only_empty_fields_and_prices_only_on_request():
    product = SimpleNamespace(sku="UZ-1", name="Сок яблочный 1 л", category="Без категории", net_content=None, country="Уже есть",
                              manufacturer="", ingredients=None, description=None, expected_price=None, in_stock=None)
    off = {"quantity": "1 L", "brands": "Bliss, X", "ingredients_text_ru": "сок яблочный, вода", "categories_tags": []}
    ch = enrich.plan_changes(product, "4780000000001", off, demo_prices=False)
    assert ch == {"category": "Напитки", "net_content": "1 л", "manufacturer": "Bliss", "ingredients": "Сок яблочный, вода"}
    ch = enrich.plan_changes(product, "4780000000001", off, demo_prices=True)
    assert ch["expected_price"] > 0 and ch["in_stock"] is True and "country" not in ch
    product.expected_price, product.in_stock = 12_000.0, False
    assert "expected_price" not in enrich.plan_changes(product, "4780000000001", off, demo_prices=True)


def test_fixes_win_over_found_values_and_archive_junk(tmp_path):
    f = tmp_path / "fixes.csv"
    f.write_text("sku;name;category;net_content;country;manufacturer;description;archive\n"
                 "UZ-1;Вода 1,5 л;;1,5 л;;;;\nUZ-2;;;;;;;1\n", encoding="utf-8-sig")
    fixes = enrich.read_fixes(f)
    assert set(fixes) == {"UZ-1", "UZ-2"} and enrich.read_fixes(tmp_path / "нет.csv") == {}
    product = SimpleNamespace(name="Water", category="Напитки", net_content=None, country=None, manufacturer=None,
                              description=None, status="active")
    ch = enrich.apply_fix(product, {"net_content": "1 л"}, fixes["UZ-1"])
    assert ch == {"net_content": "1,5 л", "name": "Вода 1,5 л"}
    assert enrich.apply_fix(product, {}, fixes["UZ-2"]) == {"status": "archived"}


def test_pick_varied_round_robin_with_cap():
    cands = [("A", {"n": i}) for i in range(10)] + [("B", {"n": 100})] + [("C", {"n": i}) for i in range(3)]
    picked = foc.pick_varied(cands, total=6, per_kind=2)
    assert [c for c, _ in picked] == ["A", "B", "C", "A", "C"]          # B кончился, у A и C не больше 2
    assert len(foc.pick_varied(cands, total=3, per_kind=5)) == 3
