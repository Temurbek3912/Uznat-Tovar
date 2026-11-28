"""ИИ-помощник в разговоре (замер 2026-11-24, ml/benchmark_assistant.py --set dialogs: верно 65 % -> 94 %).

Живая qwen3-vl-4b в разговоре из нескольких реплик: «Есть у нас пепси?» → «поменяй её цену на 15000» — программа отказывала
(товар сверялся только с последней репликой); «нет, не пепси, а фанту» — считалось вопросом; «Поставь цену фанты 9000 и пепси
15000» — вторая цена терялась молча; «Сколько стоят пепси и фанта?» — ответ только о пепси; «Сколько стоит молоко?» —
products_missing и выдуманная цена «100 сум»; «Переключи на узбекский и сделай тёмный фон» — не менялось ничего; на узбекской
кириллице — ответ латиницей. Модель подменяется: проверяется наш код."""
from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import migrate  # noqa: E402
from app.db.models import Product, ProductBarcodeORM  # noqa: E402
from app.services import assistant as A  # noqa: E402
from app.services import assistant_tools as T  # noqa: E402
from app.services import command_words as C  # noqa: E402
from tests.test_assistant import OWNER, SETTINGS, FakeModel  # noqa: E402


@pytest.fixture(name="Shop")
def _shop_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shop.sqlite'}", connect_args={"check_same_thread": False})
    migrate.upgrade_to_head(engine)
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with S() as db:
        rows = [
            Product(sku="COLA", name="Кока-кола 0,5 л", brand="Coca-Cola", category="Напитки", expected_price=9000),
            Product(sku="FANTA", name="Fanta апельсин 0,5 л", brand="Coca-Cola", category="Напитки", expected_price=8500),
            Product(sku="PEPSI", name="Pepsi 1,5 л", brand="Pepsi", category="Напитки", expected_price=14000),
            Product(sku="SNICK", name="Snickers 50 г", brand="Mars", category="Сладости", expected_price=7000),
            Product(sku="BREAD", name="Хлеб белый", brand="Не указан", category="Хлеб", expected_price=4000),
            Product(sku="MILK", name="Молоко Nestle 1 л", brand="Nestle", category="Молочные"),
        ]
        for i, p in enumerate(rows):
            p.created_at = datetime(2026, 11, 1 + i)
            db.add(p)
        db.flush()
        db.add(ProductBarcodeORM(barcode="4780001234562", product_id=rows[0].id))
        db.commit()
    yield S
    engine.dispose()


def _talk(Shop, model, *turns, lang="ru"):
    """turns — реплики по очереди: владелец, помощник, …, владелец."""
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(turns)]
    with Shop() as db:
        return A.chat(db, OWNER, history, lang, SETTINGS, http_post=model)


PROPOSED = "Проверьте предложение и нажмите «Сделать» или «Отмена»."       # что страница кладёт в историю после карточки


def test_follow_up_without_the_product_name_changes_the_product_discussed(Shop):
    price = {"tool": "propose_update", "args": {"product": "PEPSI", "field": "price", "value": 15000}}
    card = _talk(Shop, FakeModel(price), "Есть у нас пепси?", "Да, Pepsi 1,5 л — 14000 сум.",
                 "поменяй её цену на 15000").proposal
    assert card["sku"] == "PEPSI" and card["changes"] == {"price": 15000}
    # товар — из ответа помощника: «Какие товары без цены?» → «Молоко Nestle 1 л» → «поставь ему цену 12000»
    milk = {"tool": "propose_update", "args": {"product": "MILK", "field": "price", "value": 12000}}
    card = _talk(Shop, FakeModel(milk), "Какие товары без цены?", "Без цены: Молоко Nestle 1 л.",
                 "поставь ему цену 12000").proposal
    assert card["sku"] == "MILK"
    archive = {"tool": "propose_archive", "args": {"product": "Snickersni", "status": "archived"}}
    card = _talk(Shop, FakeModel(archive), "Snickers qancha turadi?", "Snickers 50 г — 7000 soʻm.", "uni arxivga oling",
                 lang="uz").proposal
    assert card["kind"] == "status" and card["sku"] == "SNICK"


def test_a_new_product_word_is_not_replaced_by_the_one_discussed(Shop):
    wrong = {"tool": "propose_update", "args": {"product": "MILK", "field": "price", "value": 9000}}
    reply = _talk(Shop, FakeModel(wrong, {"tool": "", "text": "Кефира нет."}), "Сколько стоит молоко?",
                  "Цены у молока нет.", "поставь кефиру 9000")
    assert reply.proposal is None                    # «кефиру» — другой товар (его нет), а не молоко из прошлой реплики


def test_no_not_that_one_is_a_correction_not_a_question(Shop):
    fanta = {"tool": "propose_archive", "args": {"product": "FANTA", "status": "archived"}}
    model = FakeModel(fanta)
    card = _talk(Shop, model, "Убери пепси в архив", PROPOSED, "нет, не пепси, а фанту").proposal
    assert card["kind"] == "status" and card["sku"] == "FANTA"
    schema = model.calls[0][1]["response_format"]["json_schema"]["schema"]
    assert "propose_archive" in [b["properties"]["tool"]["enum"][0] for b in schema["anyOf"]]
    pepsi = {"tool": "propose_archive", "args": {"product": "PEPSI", "status": "archived"}}
    reply = _talk(Shop, FakeModel(pepsi, {"tool": "", "text": "Уточните."}), "Убери пепси в архив", PROPOSED,
                  "нет, не пепси, а фанту")
    assert reply.proposal is None                    # «не пепси» — именно её владелец не имел в виду
    also = {"tool": "propose_update", "args": {"product": "COLA", "field": "price", "value": 9500}}
    card = _talk(Shop, FakeModel(also), "Поставь цену фанты 9000", PROPOSED, "и кока-коле тоже 9500").proposal
    assert card["sku"] == "COLA" and card["changes"] == {"price": 9500}


def test_the_one_new_price_the_owner_said_wins_over_the_models_number(Shop):
    for wrong in (12000, 1.5):                       # выдумала / взяла объём «1,5 л»
        model = FakeModel({"tool": "propose_update", "args": {"product": "PEPSI", "field": "price", "value": wrong}})
        card = _talk(Shop, model, "Поставь цену Pepsi 1,5 л 15000").proposal
        assert card["sku"] == "PEPSI" and card["changes"] == {"price": 15000}, wrong


def test_two_prices_in_one_sentence_make_one_list_card(Shop):
    model = FakeModel()                              # модель не нужна вовсе
    card = _talk(Shop, model, "Поставь цену фанты 9000 и пепси 15000").proposal
    assert card["change"] == "price_list" and {(r["sku"], r["after"]) for r in card["rows"]} == {("FANTA", 9000),
                                                                                               ("PEPSI", 15000)}
    assert model.calls == []
    card = _talk(Shop, FakeModel(), "Fanta narxini 9000, Pepsi narxini 15000 qiling", lang="uz").proposal
    assert {r["sku"] for r in card["rows"]} == {"FANTA", "PEPSI"}
    assert A.inline_prices("Подними цену фанты на 1000 и пепси на 2000") and A.inline_prices("Поставь цену фанты 9000") == ""


def test_second_product_of_a_two_product_request_is_not_lost_silently(Shop):
    archive = {"tool": "propose_archive", "args": {"product": "PEPSI", "status": "archived"}}
    reply = _talk(Shop, FakeModel(archive), "Убери в архив пепси и сникерс")
    assert reply.proposal["sku"] == "PEPSI" and "сникерс" in reply.text and "одно изменение" in reply.text
    alone = _talk(Shop, FakeModel(archive), "Убери пепси в архив")
    assert alone.proposal["sku"] == "PEPSI" and alone.text == ""


def test_a_question_about_two_products_finds_both(Shop):
    model = FakeModel({"tool": "find_products", "args": {"query": "пепси"}},
                      {"tool": "", "text": "Pepsi — 14000 сум, Fanta — 8500 сум."})
    reply = _talk(Shop, model, "Сколько стоят пепси и фанта?")
    data = model.calls[1][1]["messages"][-1]["content"]
    assert "Pepsi 1,5 л" in data and "Fanta апельсин" in data and "8500" in reply.text


def test_a_price_question_looks_for_the_product_and_no_price_is_invented(Shop):
    model = FakeModel({"tool": "products_missing", "args": {"field": "price"}},
                      {"tool": "", "text": "Молоко Nestle 1 л стоит 100 сум."},
                      {"tool": "", "text": "У молока цена не указана."})
    reply = _talk(Shop, model, "Сколько стоит молоко?")
    assert reply.tools[0]["name"] == "find_products" and reply.text == "У молока цена не указана."
    assert "«100 сум» нет в данных" in model.calls[2][1]["messages"][-1]["content"]
    count = FakeModel({"tool": "", "text": "Каталогда 6 та маҳсулот бор."})
    assert _talk(Shop, count, "Каталогда қанча маҳсулот бор?", lang="uz-Cyrl").tools[0]["name"] == "count_products"
    missing = FakeModel({"tool": "products_missing", "args": {"field": "price"}}, {"tool": "", "text": "Молоко Nestle 1 л."})
    assert _talk(Shop, missing, "У какого товара нет цены?").tools[0]["name"] == "products_missing"
    # «Нечта товар бор?» («сколько товаров?»): модель искала товар «товар» — сколько товаров, говорит сводка каталога
    count = FakeModel({"tool": "", "text": "6 та товар бор."})
    assert _talk(Shop, count, "Нечта товар бор?", lang="uz-Cyrl").tools[0]["name"] == "count_products"
    loop = FakeModel('{"tool": "", "text": "Бор бор ' + "бор " * 300)
    reply = _talk(Shop, loop, "Нечта товар бор?", lang="uz-Cyrl")
    assert reply.text == "Каталогда 6 та маҳсулот, архивда 0 та." and len(loop.calls) == 1   # счёт — программа, модель отвечает
    cocoa = FakeModel({"tool": "find_products", "args": {"query": "какао"}}, {"tool": "", "text": "Какао нет."})
    assert _talk(Shop, cocoa, "Сколько стоит какао?").tools[0] == {"name": "find_products", "ok": True}


def test_a_looping_answer_is_replaced_by_the_found_data(Shop):
    loop = '{"tool": "", "text": "Pepsi 1,5 л — 14000 сом. ' + "биринчи " * 200      # ответ оборван: JSON не дописан
    reply = _talk(Shop, FakeModel({"tool": "find_products", "args": {"query": "Пепси"}}, loop), "Пепси борми?",
                  lang="uz-Cyrl")
    assert reply.text == "Pepsi 1,5 л — 14 000 сўм."
    reply = _talk(Shop, FakeModel({"tool": "find_products", "args": {"query": "Sut"}}, loop), "Sut bormi?", lang="uz")
    assert reply.text == "«Sut» topilmadi."
    with Shop() as db:
        milk = T.find_products(db, {"query": "Молоко"})
    assert A.found_text(milk, "ru") == "Молоко Nestle 1 л — цена не указана."


def test_language_and_background_together(Shop):
    model = FakeModel({"tool": "set_language", "args": {"lang": "uz"}}, {"tool": "set_theme", "args": {"theme": "dark"}},
                      {"tool": "set_theme", "args": {"theme": "dark"}}, {"tool": "", "text": "Tayyor."})
    reply = _talk(Shop, model, "Переключи на узбекский и сделай тёмный фон")
    assert reply.ui == [{"kind": "lang", "value": "uz"}, {"kind": "theme", "value": "dark"}]
    assert C.ui_wanted("Переключи на узбекский и сделай тёмный фон") == {"lang": "uz", "theme": "dark"}
    assert C.ui_meant("Переключи на узбекский и сделай тёмный фон") is None and C.ui_meant("Сделай тёмный фон") == (
        "theme", "dark")


def test_uzbek_cyrillic_answers_are_cyrillic(Shop):
    latin = {"tool": "", "text": "5 ta mahsulot shtrixkodga ega emas."}
    model = FakeModel(latin, latin)                      # число считает программа (count_products), модель только отвечает
    reply = _talk(Shop, model, "Штрихкоди йўқ товарлар нечта?", lang="uz-Cyrl")
    assert "кириллицей" in model.calls[1][1]["messages"][-1]["content"]
    assert reply.text == "5 та маҳсулот штрихкодга эга эмас."
    assert A.tidy_text("Каталогda 6 товар, Twix — 32000 so'm.", "uz-Cyrl") == "Каталогда 6 товар, Twix — 32000 сўм."
    assert A._uz_word_cyr("yoʻq") == "йўқ"


def test_overview_names_the_most_expensive_and_a_category_question_shows_its_products(Shop):
    with Shop() as db:
        out = T.catalog_overview(db, {})
        assert out["most_expensive"][0] == {"name": "Pepsi 1,5 л", "price": 14000}
        assert out["cheapest"][0]["name"] == "Хлеб белый"
        found = T.find_products(db, {"query": "Sut"})
    assert found["total"] == 0 and found["category"] == "Молочные" and found["category_items"][0]["name"].startswith("Молоко")
    model = FakeModel({"tool": "find_products", "args": {"query": "Sut"}}, {"tool": "", "text": "Sut narxi yoʻq."})
    _talk(Shop, model, "Sut qancha turadi?", lang="uz")
    assert "propose_bulk" not in model.calls[1][1]["messages"][-1]["content"]   # вопрос: подсказка об изменении не нужна


def test_follow_up_words():
    assert C.points_back("поменяй её цену на 15000") and C.points_back("подними на 2000") and C.points_back("поставь ей 18000")
    assert C.points_back("narxini 50000 qiling") and C.points_back("uni arxivga oling")
    assert not C.points_back("поставь кефиру 9000") and not C.points_back("")
    assert C.follow_up("и кока-коле тоже 9500", "Поставь цену фанты 9000")
    assert not C.follow_up("а фанта?", "Сколько стоит пепси?") and not C.follow_up("а фанта?", "Поставь цену пепси 9000")
    assert "пепси" not in C.without_negated("нет, не пепси, а фанту") and "Pepsi" not in C.without_negated("Pepsi emas, Fanta")
