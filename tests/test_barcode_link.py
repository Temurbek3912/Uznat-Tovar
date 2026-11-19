"""Код с фото заняли, пока шла задача «Добавить товар»: товар добавлен без кода, и человеку об этом говорится
(app/services/barcode_link.py, describe_task в app/services/catalog_upload.py)."""
from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.models import Base, Product, ProductBarcodeORM  # noqa: E402
from app.services.barcode_link import link_barcodes, task_result  # noqa: E402
from app.services.catalog_upload import describe_task  # noqa: E402

FREE, TAKEN = "4780001234562", "4600000000008"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([Product(id="new", sku="NEW", name="Новый", brand="b", category="c"),
                     Product(id="other", sku="OTH", name="Кола 0,5 л", brand="b", category="c")])
    session.add(ProductBarcodeORM(barcode=TAKEN, product_id="other"))
    session.commit()
    yield session
    session.close()


def _codes(db, product_id):
    return sorted(c for (c,) in db.query(ProductBarcodeORM.barcode).filter(ProductBarcodeORM.product_id == product_id))


def test_free_code_is_linked_and_taken_code_is_reported_with_its_owner(db):
    assert link_barcodes(db, "new", [FREE, TAKEN]) == [{"code": TAKEN, "name": "Кола 0,5 л"}]
    assert _codes(db, "new") == [FREE]
    assert _codes(db, "other") == [TAKEN]            # чужой код не тронут


def test_retry_of_the_same_task_is_not_an_error(db):
    assert link_barcodes(db, "new", [FREE]) == []
    assert link_barcodes(db, "new", [FREE]) == []    # повтор задачи: код уже у этого товара
    assert _codes(db, "new") == [FREE]


def test_task_result_stays_a_plain_id_when_everything_is_linked():
    assert task_result("new", []) == "new"
    assert task_result("new", [{"code": TAKEN, "name": "Кола"}]) == {"product_id": "new",
                                                                     "barcodes_not_linked": [{"code": TAKEN, "name": "Кола"}]}


def test_describe_task_turns_not_linked_codes_into_notes():
    plain = describe_task("SUCCESS", "new")
    assert plain["product_id"] == "new" and plain["notes"] == []
    done = describe_task("SUCCESS", task_result("new", [{"code": TAKEN, "name": "Кола"}, {"code": FREE, "name": ""}]))
    assert done["state"] == "done" and done["product_id"] == "new"
    assert [n["code"] for n in done["notes"]] == ["barcode_not_linked", "barcode_not_linked_unknown"]
    assert done["notes"][0]["params"] == {"code": TAKEN, "name": "Кола"} and "Кола" in done["notes"][0]["message"]
    assert done["notes"][1]["params"] == {"code": FREE}


def test_describe_task_ignores_malformed_result():
    odd = describe_task("SUCCESS", {"product_id": 5, "barcodes_not_linked": ["x", {"code": 1}, None]})
    assert odd["product_id"] is None and odd["notes"] == []
