"""Поиск фото «не товара» (scripts/find_non_product_photos.py): разбор ответа модели, подсчёт оставшихся фото, план вывода."""
import numpy as np

from scripts.find_non_product_photos import PRODUCT, Verdict, is_suspect, label_texts, photos_left, plan, verdict


def test_label_texts_cover_products_and_not_product():
    groups, texts = label_texts()
    assert len(groups) == len(texts)
    assert PRODUCT in groups and "person" in groups and "barcode" in groups
    assert all(t.startswith("This is a photo of ") for t in texts)


def test_verdict_best_prompt_per_group_then_softmax():
    groups = [PRODUCT, PRODUCT, "person", "barcode"]
    v = verdict(groups, np.array([-5.0, 3.0, -2.0, -2.0]))     # лучшее описание товара — 3.0
    assert v.group == PRODUCT and v.not_product < 0.02
    v = verdict(groups, np.array([-5.0, -4.0, 6.0, 0.0]))
    assert v.group == "person" and v.not_product > 0.99


def test_is_suspect_threshold():
    assert is_suspect(Verdict("barcode", 0.6))
    assert not is_suspect(Verdict(PRODUCT, 0.3))
    assert is_suspect(Verdict(PRODUCT, 0.3), min_not_product=0.2)


def test_photos_left_counts_non_suspect_of_same_product():
    rows = [{"sku": "A", "suspect": 1}, {"sku": "A", "suspect": 0}, {"sku": "A", "suspect": 0}, {"sku": "B", "suspect": 1}]
    photos_left(rows)
    assert [r["left"] for r in rows] == [2, 2, 2, 0]


def test_plan_only_deactivate_rows():
    rows = [{"image_id": "1", "decision": "deactivate"}, {"image_id": "2", "decision": "keep"},
            {"image_id": "3", "decision": " Deactivate "}, {"image_id": "4", "decision": "deactivate"}, {"image_id": "5", "decision": ""}]
    images = {"1": ("p", True), "2": ("p", True), "3": ("p", False)}
    assert [(r["image_id"], o) for r, o in plan(rows, images)] == [("1", "apply"), ("3", "same"), ("4", "skip:нет такого фото")]
