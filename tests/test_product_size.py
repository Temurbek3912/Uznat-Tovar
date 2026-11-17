"""Размер товара из названия и ответ «уточните размер» (app/services/product_size.py, routes_catalog._check_size)."""
from app.api import routes_catalog
from app.services import product_namer as pn
from app.services.product_size import find_sizes, name_without_size, same_size, size_of, size_variants


def test_sizes_are_read_in_any_writing():
    assert size_of("Pepsi 0,5 л") == ("l", 0.5)
    assert size_of("PEPSI 1.5L") == ("l", 1.5)
    assert size_of("Coca-Cola 500ml") == ("l", 0.5)
    assert size_of("Fanta 330 мл") == ("l", 0.33)
    assert size_of("Hydrolife 1 литр") == ("l", 1.0)
    assert size_of("Suv 0,5 litr") == ("l", 0.5)
    assert size_of("Fanta 25cl") == ("l", 0.25)
    assert size_of("Twix 82 г (2 x 41 г)") == ("g", 82.0)
    assert size_of("Сахар 1 кг") == ("g", 1000.0)
    assert size_of("Pepsi") is None
    assert size_of("Яйца 10 шт") == ("pcs", 10.0)
    assert find_sizes("Чай 25 пакетиков 50 г") == [("g", 50.0)]
    assert same_size(size_of("0,5 л"), size_of("500 мл")) and not same_size(size_of("0,5 л"), size_of("1,5 л"))


def test_name_without_size_joins_latin_and_cyrillic():
    assert name_without_size("Pepsi 0,5 л") == name_without_size("Пепси 1,5 литра") == {"pepsi"}


def test_size_variants_only_same_name_other_size():
    first = {"id": "p05", "name": "Pepsi 0,5 л", "brand": "Pepsi"}
    others = [first, {"id": "p15", "name": "Pepsi 1,5 л", "brand": "Pepsi"},
              {"id": "z15", "name": "Pepsi Zero 1,5 л", "brand": "Pepsi"},          # другой вкус — не размер
              {"id": "p05b", "name": "Pepsi 500 мл", "brand": "Pepsi"},              # тот же размер другой записью
              {"id": "c15", "name": "Coca-Cola 1,5 л", "brand": "Coca-Cola"}]
    assert [c["id"] for c in size_variants(first, others)] == ["p15"]
    assert size_variants({"id": "x", "name": "Pepsi", "brand": ""}, others) == []      # у первого размер не указан


SIZES = {"status": "matched", "by": "look", "score": 0.91,
         "product": {"id": "p05", "name": "Pepsi 0,5 л", "brand": "Pepsi", "score": 0.91},
         "candidates": [{"id": "p05", "name": "Pepsi 0,5 л", "brand": "Pepsi", "score": 0.91},
                        {"id": "p15", "name": "Pepsi 1,5 л", "brand": "Pepsi", "score": 0.88},
                        {"id": "c05", "name": "Coca-Cola 0,5 л", "brand": "Coca-Cola", "score": 0.70}]}


def _label(monkeypatch, guess):
    monkeypatch.setattr(pn, "read_label", lambda *a, **k: guess)
    monkeypatch.setattr(pn, "agreed_kind", lambda g, *a, **k: g)


def test_size_read_on_label_puts_that_size_first(monkeypatch):
    _label(monkeypatch, pn.Guess("Pepsi 1,5 л", "Pepsi", "vision", sure=True))
    out = routes_catalog.add_outside_name(dict(SIZES), b"j", [], "ru")
    assert out["status"] == "matched" and out["product"]["id"] == "p15" and out["size"] == "label"
    assert [c["id"] for c in out["candidates"]] == ["p15", "p05", "c05"]


def test_size_not_read_asks_to_choose_between_sizes(monkeypatch):
    _label(monkeypatch, None)                                                     # модель не уверена или её нет
    out = routes_catalog.add_outside_name(dict(SIZES), b"j", [], "ru")
    assert out["status"] == "uncertain" and out["size"] == "choose" and out["product"]["id"] == "p05"
    assert [c["id"] for c in out["candidates"]] == ["p05", "p15", "c05"]
    _label(monkeypatch, pn.Guess("Pepsi", "Pepsi", "vision", sure=True))          # марка прочитана, размер — нет
    assert routes_catalog.add_outside_name(dict(SIZES), b"j", [], "ru")["size"] == "choose"


def test_far_size_or_barcode_answer_is_untouched(monkeypatch):
    _label(monkeypatch, None)
    far = {**SIZES, "candidates": [SIZES["candidates"][0], {**SIZES["candidates"][1], "score": 0.70}]}
    assert routes_catalog.add_outside_name(far, b"j", [], "ru") is far             # другой размер далеко по счёту
    by_code = {**SIZES, "by": "barcode"}
    assert routes_catalog.add_outside_name(by_code, b"j", [], "ru") is by_code     # по штрихкоду размер точный
