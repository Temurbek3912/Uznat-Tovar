"""Вопросы владельца ко ИИ-помощнику на всех языках (замер 2026-11-28, ml/benchmark_assistant.py --set owner,
docs/benchmark/assistant_owner_questions_2026-11-28.md). Набор и проверка «Сделать»/«Отменить» на временной базе — без модели."""
from __future__ import annotations

from collections import Counter

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

from app.core.security import CurrentUser, Role  # noqa: E402
from app.db.models import Product  # noqa: E402
from app.services import assistant_actions  # noqa: E402
from ml import benchmark_assistant as B  # noqa: E402
from ml.assistant_owner_questions import DIALOGS  # noqa: E402

OWNER = CurrentUser(id="u1", username="owner", role=Role.ADMIN)


def test_the_set_covers_every_kind_in_every_language():
    assert len({d["id"] for d in DIALOGS}) == len(DIALOGS)
    langs = Counter((d["id"].split("_")[0], t["lang"]) for d in DIALOGS for t in d["turns"])
    for kind in ("find", "count", "missing", "add", "upd", "arch", "copy", "bulk", "list", "ui", "follow"):
        assert all(langs[(kind, lang)] for lang in ("ru", "uz", "uz-Cyrl", "en")), kind
    for d in DIALOGS:
        for t in d["turns"]:
            assert t["lang"] in ("ru", "uz", "uz-Cyrl", "en")
            assert not t.get("db") or d.get("apply"), d["id"]      # «Сделать» — только на своей временной базе


@pytest.fixture(name="Shop")
def _shop(tmp_path):
    engine, session = B.make_catalog(tmp_path / "c.sqlite")
    yield session
    engine.dispose()


def _card(session, args):
    props = assistant_actions.Proposals()
    with session() as db:
        props.update(db, args)
    return props.items[0]


def test_pressing_do_changes_the_temporary_base_and_undo_restores_it(Shop):
    card = _card(Shop, {"product": "FANTA", "field": "price", "value": "9000"})
    assert B.apply_problems(Shop, OWNER, card, {"FANTA": {"price": 9000}}, undo=True) == []
    with Shop() as db:
        assert db.query(Product).filter(Product.sku == "FANTA").one().expected_price == 8500     # «Отменить» вернул цену


def test_a_wrong_result_in_the_base_is_reported(Shop):
    card = _card(Shop, {"product": "FANTA", "field": "price", "value": "9000"})
    problems = B.apply_problems(Shop, OWNER, card, {"FANTA": {"price": 9500}}, undo=False)
    assert problems and "FANTA.price" in problems[0]


def test_bulk_card_and_barcode_are_applied(Shop):
    with Shop() as db:
        bulk = assistant_actions.bulk_card(db, {"where": "category", "query": "Снеки", "change": "price_add", "value": "1000"})
    assert B.apply_problems(Shop, OWNER, bulk, {"PRING": {"price": 33000}, "LAYS": {"price": 13000}}, undo=True) == []
    code = _card(Shop, {"product": "PEPSI", "field": "add_barcode", "value": "4870001234560"})
    assert B.apply_problems(Shop, OWNER, code, {"PEPSI": {"barcode": "4870001234560"}}, undo=True) == []


def test_new_product_card_fields_are_checked():
    assert B.card_problems({"kind": "add", "name": "Sprite 0,5 л", "price": 8500.0}, {"name": "sprite", "price": 8500}) == []
    assert B.card_problems({"kind": "add", "name": "Sprite", "price": 850.0}, {"price": 8500})
    assert B.card_problems(None, {"name": "x"}) == ["нет карточки"]


# --- исправления по замеру 2026-11-28 (модель подменяется: проверяется наш код) --------------------------------------------------

from app.services import assistant as A  # noqa: E402
from app.services import command_words as C  # noqa: E402
from tests.test_assistant import SETTINGS, FakeModel  # noqa: E402


def _ask(Shop, model, said, lang="ru", history=()):
    messages = [*history, {"role": "user", "content": said}]
    with Shop() as db:
        return A.chat(db, OWNER, messages, lang, SETTINGS, http_post=model)


def _update(product, field, value):
    return {"tool": "propose_update", "args": {"product": product, "field": field, "value": value}}


def test_cheaper_or_raise_by_a_sum_is_not_a_new_price(Shop):
    """«сделай сникерс подешевле на 1000» → 6000, а не 1000; «Нутелла нархини 1000 га камайтиринг» (модель назвала товар словом,
    а не артикулом) → 44000; «Raise the price of Fanta by 500» → 9000."""
    for said, lang, call, price in (
            ("сделай сникерс подешевле на 1000", "ru", _update("SNICK", "price", 1000), 6000),
            ("Нутелла нархини 1000 га камайтиринг", "uz-Cyrl", _update("Нутелла", "price", 1000), 44000),
            ("Raise the price of Fanta by 500", "en", _update("FANTA", "price", 500), 9000),
            ("фанта подорожала на 500", "ru", _update("FANTA", "price", 500), 9000)):
        reply = _ask(Shop, FakeModel(call), said, lang)
        assert reply.proposal["changes"] == {"price": price}, said


def test_removing_a_barcode_is_not_archiving_the_product(Shop):
    reply = _ask(Shop, FakeModel({"tool": "propose_archive", "args": {"product": "FANTA", "status": "archived"}}),
                 "убери штрихкод у фанты")
    assert reply.proposal["kind"] == "update" and reply.proposal["changes"] == {"remove_barcode": "4780001234579"}


def test_copy_does_not_change_the_original(Shop):
    model = FakeModel(_update("NUT", "net_content", "750 г"), {"tool": "", "text": "Копировать товар я не умею."})
    reply = _ask(Shop, model, "Сделай копию нутеллы, только 750 г")
    assert reply.proposal is None and "Копировать" in reply.proposal_error


def test_rename_brand_and_category_phrases_are_parsed_by_the_program(Shop):
    for said, lang, sku, field, value in (
            ("Пепси номини Pepsi Cola қилинг", "uz-Cyrl", "PEPSI", "name", "Pepsi Cola"),
            ("Pepsi nomini Pepsi Cola 1,5 l ga oʻzgartiring", "uz", "PEPSI", "name", "Pepsi Cola 1,5 l"),
            ("Rename Pepsi 1.5 l to Pepsi Cola 1.5 l", "en", "PEPSI", "name", "Pepsi Cola 1.5 l"),
            ("Set brand of Snickers to Mars Inc", "en", "SNICK", "brand", "Mars Inc"),
            ("Move Hydrolife water to category Water", "en", "WATER", "category", "Water")):
        reply = _ask(Shop, FakeModel(), said, lang)              # модель не спрашивается вовсе
        assert reply.proposal["sku"] == sku and reply.proposal["changes"] == {field: value}, said


def test_all_of_a_category_to_or_from_the_archive_is_parsed_by_the_program(Shop):
    for said, lang, change, cat in (("Барча газакларни архивга олинг", "uz-Cyrl", "archive", "Снеки"),
                                    ("Barcha gazaklarni arxivga oling", "uz", "archive", "Снеки"),
                                    ("Archive all snacks", "en", "archive", "Снеки"),
                                    ("Верни из архива все напитки", "ru", "restore", "Напитки")):
        reply = _ask(Shop, FakeModel(), said, lang)
        assert reply.proposal["change"] == change and reply.proposal["categories"] == [cat], said
    with Shop() as db:
        for said in ("Удали все товары", "Hamma tovarlarni oʻchiring", "Barcha ichimliklar narxini 10% oshiring"):
            assert A.bulk_status_meant(db, [{"role": "user", "content": said}]) is None, said


def test_english_category_words_and_questions(Shop):
    with Shop() as db:
        assert A.count_args(db, "How many drinks do we have?") == {"category": "Напитки"}
        assert A.count_args(db, "how many snacks") == {"category": "Снеки"}
        assert A.count_args(db, "How many products have no price?") == {"without": "price"}
        assert A.missing_args(db, "Products with no price?") == {"field": "price"}
        assert A.missing_args(db, "Which products have no photo?") == {"field": "photo"}
        assert A.count_args(db, "Сколько товаров дороже 10000?") == {"price_from": 10001}
        assert A.count_args(db, "Coca-Cola qancha?") is None              # цена, а не число товаров бренда
        assert A.count_args(db, "Coca-Cola mahsulotlari nechta?") == {"brand": "Coca-Cola"}
    bulk = {"tool": "propose_bulk", "args": {"where": "category", "query": "Напитки", "change": "price_percent", "value": 10}}
    reply = _ask(Shop, FakeModel(bulk), "Raise prices of all drinks by 10%", "en")
    assert reply.proposal["kind"] == "bulk" and reply.proposal["categories"] == ["Напитки"]


def test_brand_products_mean_all_of_the_brand(Shop):
    bulk = {"tool": "propose_bulk", "args": {"where": "brand", "query": "Coca-Cola", "change": "price_percent", "value": 5}}
    reply = _ask(Shop, FakeModel(bulk), "Coca-Cola mahsulotlari narxini 5% ga tushiring", "uz")
    assert reply.proposal["kind"] == "bulk" and reply.proposal["value"] == -5


def test_statements_that_are_changes():
    assert C.wants_change("Нутелла теперь стоит 46 тысяч") and C.wants_change("фанта подорожала на 500")
    assert C.wants_change("молоко снова есть в наличии") and C.stock_in_said("молоко снова есть в наличии")
    assert C.wants_change("Move Hydrolife water to category Water") and C.wants_change("Rename Pepsi")
    for question in ("Сникерс есть в наличии?", "кока кола скока стоит", "Сколько стоит Coca-Cola 0,5", "Сколько стоит нутелла 400"):
        assert not C.wants_change(question), question
    assert C.points_back("set its price to 50000")


def test_english_claim_without_a_card_is_caught():
    assert A.claims_change("The discount has been applied to all Coca-Cola products.")


def test_a_computed_price_from_the_model_is_recounted(Shop):
    """«фанта подорожала на 500»: модель дала цену 13500 — новую цену считает программа по словам владельца (8500 + 500)."""
    for value in (13500, 12750, "500"):
        reply = _ask(Shop, FakeModel(_update("FANTA", "price", value)), "фанта подорожала на 500")
        assert reply.proposal["changes"] == {"price": 9000}, value


def test_no_not_pepsi_but_fanta_keeps_the_price_request(Shop):
    history = [{"role": "user", "content": "Пепси нархини 15000 қилинг"},
               {"role": "assistant", "content": "Таклифни текширинг ва «Бажариш» ни босинг."}]
    reply = _ask(Shop, FakeModel(_update("FANTA", "in_stock", "йўқ")), "йўқ, Пепси эмас, Фантани", "uz-Cyrl", history)
    assert reply.proposal["sku"] == "FANTA" and reply.proposal["changes"] == {"price": 15000}


def test_one_named_product_to_archive_back_or_out_of_stock_is_parsed_by_the_program(Shop):
    for said, lang, kind, sku, what in (("Сникерсни ўчиринг", "uz-Cyrl", "status", "SNICK", "archived"),
                                        ("Dobriy sharbatni qaytaring", "uz", "status", "JUICE", "active"),
                                        ("Delete Snickers", "en", "status", "SNICK", "archived"),
                                        ("Pepsi qolmadi", "uz", "update", "PEPSI", False),
                                        ("молоко снова есть в наличии", "ru", "update", "MILK", True)):
        reply = _ask(Shop, FakeModel(), said, lang)
        card = reply.proposal
        assert card["kind"] == kind and card["sku"] == sku, said
        assert (card["status"] if kind == "status" else card["changes"]["in_stock"]) == what, said
    with Shop() as db:
        for said in ("Удали товар Спрайт", "Сколько товаров в архиве?", "Удалить фото у хлеба", "нет, не пепси, а фанту",
                     "Сникерс есть в наличии?", "Убери в архив пепси и сникерс"):
            assert A.status_meant(db, [{"role": "user", "content": said}]) is None, said


def test_a_barcode_for_an_existing_product_is_not_a_new_product(Shop):
    add = {"tool": "propose_add", "args": {"name": "Pepsi 1,5 л"}}
    reply = _ask(Shop, FakeModel(add), "Pepsiga 4870001234560 shtrixkodini qoʻshing", "uz")
    assert reply.proposal["sku"] == "PEPSI" and reply.proposal["changes"] == {"add_barcode": "4870001234560"}


def test_a_missing_product_is_answered_as_not_found(Shop):
    model = FakeModel({"tool": "find_products", "args": {"query": "Sprite"}}, {"tool": "", "text": "Sprite topilmadi."})
    reply = _ask(Shop, model, "Sprite bormi?", "uz")
    assert reply.text == "Sprite topilmadi." and len(model.calls) == 2
    assert model.calls[1][1]["response_format"]["json_schema"]["schema"]["properties"]["tool"]["enum"] == [""]
