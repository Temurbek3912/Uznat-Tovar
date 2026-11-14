"""Инструмент замера «назвать товар вне базы» (ml/benchmark_naming.py): отбор товаров, оценка ответов, пороги, интервалы.
Сеть и модель не нужны."""
import csv
import io

from PIL import Image

from ml import benchmark_naming as bn


def _p(code, name, brands, image="https://images.openfoodfacts.org/x/front_ru.3.400.jpg"):
    return {"code": code, "product_name": name, "brands": brands, "image_front_url": image}


def test_pick_products_skips_catalog_brands_incomplete_rows_and_limits_each_brand():
    products = [
        _p("1", "Coca-Cola Zero", "Coca-Cola"),          # тестовая база магазина — не «вне базы»
        _p("2", "Nutella", "Ferrero, Nutella"),
        _p("3", "", "Alpen Gold"),                        # без названия
        _p("4", "Шоколад Alpen Gold", "Alpen Gold", image=""),
        _p("5", "Alpen Gold молочный", "Alpen Gold"),
        _p("6", "Alpen Gold фундук", "Alpen Gold"),
        _p("7", "Alpen Gold изюм", "Alpen Gold"),         # третий той же марки — лишний
        _p("8", "Lay's сметана", "Lay's"),
    ]
    got = bn.pick_products(products, {}, set(), limit=10)
    assert [g["code"] for g in got] == ["5", "6", "8"]
    assert got[0]["image_url"].endswith("front_ru.3.full.jpg")          # полный размер, не 400 точек


def test_auto_verdict_by_brand_in_latin_or_cyrillic_and_negatives():
    row = {"kind": "uzbekistan", "brand": "Alpen Gold", "brands_all": "Alpen Gold, Mondelez", "name": "Шоколад", "name_en": ""}
    assert bn.auto_verdict(row, {"is_product": True, "name": "Шоколад Альпен Голд", "brand": ""}) == "correct"
    assert bn.auto_verdict(row, {"is_product": True, "name": "Шоколад Milka", "brand": "Milka"}) == "wrong"
    assert bn.auto_verdict(row, {"is_product": False, "name": "", "brand": ""}) == "none"
    assert bn.auto_verdict(row, None) == "none"
    negative = {"kind": "negative", "brand": "", "brands_all": "", "name": "", "name_en": ""}
    assert bn.auto_verdict(negative, {"is_product": True, "name": "Шоколад", "brand": ""}) == "wrong"


def test_outcome_applies_the_threshold_like_the_app_and_manual_verdict_wins():
    row = {"is_product": "True", "answer_name": "Чай Ahmad", "answer_brand": "Ahmad", "confidence": "medium",
           "auto": "correct", "verdict": ""}
    assert bn.outcome(row, "medium") == "correct"
    assert bn.outcome(row, "high") == "none"
    assert bn.outcome({**row, "verdict": "wrong"}, "medium") == "wrong"
    assert bn.outcome({**row, "is_product": "False"}, "medium") == "none"


def test_wilson_interval_is_honest_at_zero_errors():
    lo, hi = bn.wilson(0, 50)
    assert lo == 0.0 and 0.05 < hi < 0.09          # ноль ошибок из 50 — это не «0 %», а «до ~7 %»
    assert bn.wilson(0, 0) == (0.0, 1.0)


def test_summarize_counts_three_outcomes_per_threshold():
    rows = [
        {"variant": "orig", "is_product": "True", "answer_name": "A", "answer_brand": "", "confidence": "high", "auto": "correct", "verdict": ""},
        {"variant": "orig", "is_product": "True", "answer_name": "B", "answer_brand": "", "confidence": "medium", "auto": "wrong", "verdict": ""},
        {"variant": "orig", "is_product": "True", "answer_name": "C", "answer_brand": "", "confidence": "low", "auto": "wrong", "verdict": ""},
    ]
    table = {t["threshold"]: t for t in bn.summarize(rows)}
    assert (table["high"]["correct"], table["high"]["wrong"], table["high"]["none"]) == (1, 0, 2)
    assert (table["medium"]["correct"], table["medium"]["wrong"], table["medium"]["none"]) == (1, 1, 1)


def test_shopper_variant_is_deterministic_and_a_valid_jpeg():
    src = io.BytesIO()
    Image.new("RGB", (600, 800), "blue").save(src, format="JPEG")
    a, b = bn.make_shopper(src.getvalue(), 42), bn.make_shopper(src.getvalue(), 42)
    assert a == b
    with Image.open(io.BytesIO(a)) as im:
        assert im.format == "JPEG" and im.width > 0


def test_report_writes_markdown(tmp_path):
    rows = [{"file": "orig/1.jpg", "code": "1", "name": "Чай", "brand": "Ahmad", "brands_all": "Ahmad", "name_en": "",
             "kind": "world", "variant": "orig", "answer_name": "Чай Lipton", "answer_brand": "Lipton", "is_product": "True",
             "confidence": "high", "seconds": "1.5", "auto": "wrong", "verdict": ""}]
    with open(tmp_path / bn.RESULTS, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    md = tmp_path / "r.md"
    assert bn.main(["report", "--data", str(tmp_path), "--md", str(md)]) == 0
    text = md.read_text(encoding="utf-8")
    assert "Чай Lipton" in text and "Ahmad" in text
