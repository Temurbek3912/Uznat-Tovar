"""Вопросы «сколько» (этап 1 плана docs/IMPROVEMENT_PLAN_2026-11-28.md): счёт делает программа (count_products), а не модель.

Владелец: на «сколько у нас видов сладостей» помощник ответил 2 — find_products нашёл слово «сладости» в названиях двух товаров,
а в категории «Сладости» их 107."""
from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import migrate  # noqa: E402
from app.db.models import PRODUCT_ARCHIVED, Product, ProductBarcodeORM  # noqa: E402
from app.services import assistant as A  # noqa: E402
from app.services import assistant_tools as T  # noqa: E402
from tests.test_assistant import OWNER, SETTINGS, FakeModel  # noqa: E402


@pytest.fixture(name="Shop")
def _shop(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shop.sqlite'}", connect_args={"check_same_thread": False})
    migrate.upgrade_to_head(engine)
    S = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    rows = [
        ("S1", "Конфеты Умные сладости 200 г", "Умные сладости", "Сладости", 12000),
        ("S2", "Козинак «Восточные сладости»", "Не указан", "Сладости", None),
        ("S3", "Alpen Gold фундук 90 г", "Alpen Gold", "Сладости", 15000),
        ("S4", "Alpen Gold молочный 85 г", "Alpen Gold", "Сладости", 14000),
        ("S5", "Alpen gold max fun 160 г", "Mondelez", "Сладости", 21000),
        ("S6", "Snickers 50 г", "Mars", "Сладости", 7000),
        ("D1", "Coca-Cola Classic 0,5 л", "Coca-Cola", "Напитки", 9000),
        ("D2", "CocaCola 1 л", "Не указан", "Напитки", 11000),
        ("D3", "Вода Nestlé 0,5 л", "Nestlé", "Напитки", 3000),
        ("D4", "Чай Lipton 25 пак.", "Lipton", "Чай и кофе", 18000),
        ("M1", "Кефир 1 л", "Nestle", "Молочные продукты", None),
        ("M2", "Молоко 1 л", "Простоквашино", "Молочные продукты", 13000),
    ]
    with S() as db:
        for n, (sku, name, brand, cat, price) in enumerate(rows):
            p = Product(sku=sku, name=name, brand=brand, category=cat, expected_price=price,
                        created_at=datetime(2026, 11, 1, 0, n))
            db.add(p)
            db.flush()
            if not sku.startswith("S"):
                db.add(ProductBarcodeORM(barcode=f"47800000{n:05d}", product_id=p.id))
        db.add(Product(sku="OLD", name="Старый товар", brand="Не указан", category="Сладости", status=PRODUCT_ARCHIVED))
        db.commit()
    yield S
    engine.dispose()


def _talk(Shop, model, said, lang="ru"):
    with Shop() as db:
        return A.chat(db, OWNER, [{"role": "user", "content": said}], lang, SETTINGS, http_post=model)


def test_count_products_counts_by_category_brand_missing_and_price(Shop):
    with Shop() as db:
        sweets = T.count_products(db, {"category": "сладости"})
        assert sweets["total"] == 6 and sweets["summary"] == "Товаров в категории «Сладости»: 6." and len(sweets["items"]) == 5
        assert sweets["most_expensive"] == {"name": "Alpen gold max fun 160 г", "price": 21000}
        assert T.count_products(db, {"category": "shirinliklar", "_lang": "uz"})["summary"] == \
            "«Сладости» kategoriyasidagi mahsulotlar: 6 ta."
        # бренд — тот же (без регистра и «é») или он в названии: «Alpen gold max fun» с брендом Mondelez — тоже Alpen Gold
        assert T.count_products(db, {"brand": "alpen gold"})["total"] == 3
        assert T.count_products(db, {"brand": "Coca Cola"})["total"] == 2
        assert T.count_products(db, {"brand": "Nestle"})["total"] == 2
        assert T.count_products(db, {"without": "price"})["total"] == 2
        assert T.count_products(db, {"category": "Сладости", "without": "barcode"})["total"] == 6
        assert T.count_products(db, {"price_to": 10000})["total"] == 3
        assert T.count_products(db, {"category": "Напитки", "price_from": "10 000"})["total"] == 1
        everything = T.count_products(db, {})
        assert everything["summary"] == "Товаров в каталоге: 12, в архиве: 1." and everything["categories_total"] == 4
        missing = T.count_products(db, {"category": "Игрушки"})
        assert missing["total"] == 0 and "нет" in missing["summary"]
        with pytest.raises(T.ToolError):
            T.count_products(db, {"without": "smell"})


def test_find_products_answers_about_the_category_when_a_category_is_named(Shop):
    """«сладости» есть в названиях двух товаров — но это категория: ответ о её 6 товарах, совпадения по названию — отдельно."""
    with Shop() as db:
        found = T.find_products(db, {"query": "сладости"})
        assert found["total"] == 6 and found["category"] == "Сладости" and found["category_total"] == 6
        assert found["by_name_total"] == 2 and "6" in found["summary"]
        kefir = T.find_products(db, {"query": "кефир"})            # один товар, а не вся категория «Молочные продукты»
        assert kefir["total"] == 1 and "category_total" not in kefir


def test_count_questions_are_recognised_and_price_questions_are_not():
    for said in ("Сколько у нас видов сладостей?", "Shirinliklar nechta?", "Газаклар қанча?", "Нечта ичимлик бор?",
                 "Сколько товаров без цены?", "Shirinliklardan nechtasining narxi yoʻq?", "Сколько товаров в архиве?",
                 "Сколько товаров с ценой до 5000 сум?", "How many drinks?"):
        assert A.asks_count(said), said
    for said in ("Сколько стоит пепси?", "Sut qancha turadi?", "Pepsi narxi qancha?", "Нутелла қанча туради?",
                 "Поставь цену 5000 всем сладостям", "Есть у нас пепси?"):
        assert not A.asks_count(said), said
    assert A.asks_top("Какой самый дорогой напиток?") and A.asks_top("Eng qimmat ichimlik qaysi?")


def test_count_arguments_come_from_the_owners_words(Shop):
    with Shop() as db:
        assert A.count_args(db, "Сколько у нас видов сладостей?") == {"category": "Сладости"}
        assert A.count_args(db, "Сут маҳсулотлари нечта?") == {"category": "Молочные продукты"}
        assert A.count_args(db, "Сколько сладостей без цены?") == {"category": "Сладости", "without": "price"}
        assert A.count_args(db, "Сколько у нас товаров Alpen Gold?") == {"brand": "Alpen Gold"}
        assert A.count_args(db, "Сколько напитков дороже 10 тысяч?") == {"category": "Напитки", "price_from": 10001}
        assert A.count_args(db, "Narxi 5000 soʻmgacha mahsulotlar nechta?") == {"price_to": 5000}
        assert A.count_args(db, "Жами нечта маҳсулот бор?") == {}
        assert A.count_args(db, "Сколько у нас шоколада?") is None      # товар, а не категория: ищет модель, как раньше


def test_sweets_question_gets_the_category_number_even_if_the_model_says_2(Shop):
    """Модель сказала «2» — в данных count_products 6: ответ — готовая фраза программы. Модель не выбирает инструмент."""
    model = FakeModel({"tool": "", "text": "У нас 2 вида сладостей."})
    reply = _talk(Shop, model, "Сколько у нас видов сладостей?")
    assert reply.text == "Товаров в категории «Сладости»: 6." and reply.tools == [{"name": "count_products", "ok": True}]
    assert len(model.calls) == 1 and '"total": 6' in model.calls[0][1]["messages"][-1]["content"]
    good = FakeModel({"tool": "", "text": "Shirinliklar 6 ta."})
    assert _talk(Shop, good, "Shirinliklar nechta?", lang="uz").text == "Shirinliklar 6 ta."
    none = FakeModel({"tool": "", "text": "Bunday mahsulot yoʻq."})
    assert _talk(Shop, none, "Narxi 1000 soʻmgacha mahsulotlar nechta?", lang="uz").text == "Bunday mahsulot yoʻq."


def test_most_expensive_in_a_category(Shop):
    model = FakeModel({"tool": "", "text": "Самый дорогой — Кефир."})
    reply = _talk(Shop, model, "Какой самый дорогой напиток?")
    assert reply.text == "Самый дорогой: CocaCola 1 л — 11 000 сум."
    ok = FakeModel({"tool": "", "text": "Eng qimmat ichimlik — CocaCola 1 л, 11 000 soʻm."})
    assert "CocaCola" in _talk(Shop, ok, "Eng qimmat ichimlik qaysi?", lang="uz").text


def test_count_products_is_not_offered_to_the_model():
    names = [t.name for t in T.allowed_tools(OWNER)]
    assert "count_products" in names
    assert "count_products" not in A.system_prompt(OWNER, T.allowed_tools(OWNER), "ru")
