"""Карточка товара для покупателя (app/services/shop_card.py): выбор полей магазином, срок годности — никогда."""
from __future__ import annotations

import json
import types

import pytest

from app.services import shop_card as sc


def product(**kw):
    base = dict(name="Кола", brand="Coca-Cola", category="Напитки", expected_price=9900.0, manufacturer=None, country=None,
                net_content=None, ingredients=None, description=None, in_stock=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_validate_keeps_card_order_and_drops_duplicates():
    assert sc.validate_fields(["description", "PRICE", "price", " country "]) == ("price", "country", "description")
    assert sc.validate_fields([]) == ()


@pytest.mark.parametrize("code", ["expiry", "expiry_date", "best_before", "shelf_life", "srok_godnosti", "Expiration_Date", "my_expiry"])
def test_expiry_can_never_be_enabled(code):
    with pytest.raises(sc.CardFieldsError) as err:
        sc.validate_fields(["price", code])
    assert err.value.code == "card_field_expiry_forbidden" and "никогда" in str(err.value)


def test_unknown_or_bad_input_is_refused():
    with pytest.raises(sc.CardFieldsError) as err:
        sc.validate_fields(["price", "barcode"])
    assert err.value.code == "card_field_unknown"
    with pytest.raises(sc.CardFieldsError) as err:
        sc.validate_fields("price")
    assert err.value.code == "card_fields_invalid"


def test_no_expiry_anywhere_in_the_field_lists():
    for codes in (sc.CARD_FIELDS, sc.DEFAULT_FIELDS, sc.TEXT_FIELDS):
        assert not [c for c in codes if "expir" in c or "srok" in c or "best" in c]


def test_stored_value_default_broken_and_hand_edited():
    assert sc.parse_stored(None) == sc.DEFAULT_FIELDS and sc.parse_stored("  ") == sc.DEFAULT_FIELDS
    assert sc.parse_stored("not json") == sc.DEFAULT_FIELDS and sc.parse_stored('{"a": 1}') == sc.DEFAULT_FIELDS
    assert sc.parse_stored("[]") == ()                                          # магазин выключил всё — так и есть
    # срок годности, вписанный в базу руками, всё равно не показывается
    assert sc.parse_stored(json.dumps(["expiry_date", "country", "price"])) == ("price", "country")
    assert sc.parse_stored(sc.dump_fields(("price", "in_stock"))) == ("price", "in_stock")


def test_card_has_only_enabled_and_non_empty_fields():
    p = product(country="Узбекистан", manufacturer="  ", ingredients="вода, сахар", in_stock=False, net_content="0,5 л")
    assert sc.card(p, ("price", "country", "in_stock")) == {
        "name": "Кола", "brand": "Coca-Cola", "category": "Напитки", "price": 9900.0,
        "details": {"country": "Узбекистан", "in_stock": False}}
    no_price = sc.card(p, ("manufacturer", "ingredients"))
    assert "price" not in no_price and no_price["details"] == {"ingredients": "вода, сахар"}
    assert sc.card(product(expected_price=None, in_stock=None), ("price", "in_stock")) == {
        "name": "Кола", "brand": "Coca-Cola", "category": "Напитки", "price": None, "details": {}}


def test_clean_detail():
    assert sc.clean_detail("country", "  Узбекистан \n ") == "Узбекистан"
    assert sc.clean_detail("manufacturer", "   ") is None
    assert sc.clean_detail("ingredients", " вода,\nсахар ") == "вода,\nсахар"          # в составе переносы строк остаются
    with pytest.raises(sc.CardFieldsError) as err:
        sc.clean_detail("country", "x" * (sc.MAX_SHORT + 1))
    assert err.value.code == "product_detail_too_long"
    assert sc.clean_detail("description", "x" * sc.MAX_LONG) == "x" * sc.MAX_LONG


def test_placeholder_brand_and_category_are_not_shown_to_customers():
    out = sc.card(product(brand="Не указан", category="Без категории"), ())
    assert out["brand"] == "" and out["category"] == "" and out["details"] == {}
    assert sc.card(product(brand="  Roshen ", category="Сладости"), ())["brand"] == "Roshen"
