"""app/services/identify_service.py — «покажу товар, система называет его». Выполняется офлайн.

Эмбеддер здесь ПОДДЕЛЬНЫЙ (направление в цветовом пространстве), индекс и матчер — настоящие. Тест проверяет логику решения и
проводку (статусы, запасные варианты, удалённые товары), а НЕ качество распознавания настоящей моделью DINOv2 — оно не проверялось.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.pipeline.matcher import NumpyCatalogIndex, ProductMatcher
from app.services.identify_service import (CANDIDATES_SHOWN, ProductInfo, add_reference_vectors, identify)


class ColorEmbedder:
    """Средний цвет кадра -> единичный вектор. Разные цвета — разные направления; серый кадр -> нулевой вектор."""

    embedding_dim = 3

    def embed(self, crops):
        out = []
        for crop in crops:
            v = crop.reshape(-1, 3).mean(axis=0) - 127.5
            n = np.linalg.norm(v)
            out.append(v / n if n > 1e-6 else np.zeros(3))
        return np.array(out, dtype=np.float32)


def _img(r, g, b):
    return np.full((20, 20, 3), (r, g, b), dtype=np.uint8)


PRODUCTS = {
    "p-cola": ProductInfo("p-cola", "Кола 0,5 л", "BrandCola", 9900.0),
    "p-lime": ProductInfo("p-lime", "Лимонад лайм", "BrandCola", None),
    "p-sky": ProductInfo("p-sky", "Вода голубая", "Aqua", 3000.0),
}


def _lookup(ids):
    return {i: PRODUCTS[i] for i in ids if i in PRODUCTS}


def _catalog(**matcher_kwargs):
    embedder, index = ColorEmbedder(), NumpyCatalogIndex()
    index.add("p-cola", embedder.embed([_img(220, 30, 30), _img(200, 40, 40)]))     # красный — 2 фото
    index.add("p-lime", embedder.embed([_img(30, 220, 30)]))                           # зелёный
    index.add("p-sky", embedder.embed([_img(30, 30, 220)]))                            # синий
    matcher = ProductMatcher(index, match_threshold=0.9, unknown_threshold=0.5, **matcher_kwargs)
    return embedder, matcher


def test_a_shown_product_is_named_with_its_details():
    embedder, matcher = _catalog()
    result = identify(_img(210, 35, 35), embedder, matcher, _lookup)
    assert result["status"] == "matched"
    assert result["product"] == {"id": "p-cola", "name": "Кола 0,5 л", "brand": "BrandCola", "expected_price": 9900.0, "score": result["score"]}
    assert result["score"] > 0.99
    assert result["candidates"][0]["name"] == "Кола 0,5 л" and 1 <= len(result["candidates"]) <= CANDIDATES_SHOWN


def test_each_catalog_product_is_recognised_one_after_another():
    embedder, matcher = _catalog()
    shown = [(_img(215, 33, 33), "Кола 0,5 л"), (_img(33, 215, 33), "Лимонад лайм"), (_img(33, 33, 215), "Вода голубая")]
    assert [identify(img, embedder, matcher, _lookup)["product"]["name"] for img, _ in shown] == [name for _, name in shown]


def test_an_unfamiliar_thing_is_unknown_not_a_wrong_name():
    embedder, matcher = _catalog()
    for img in (_img(127, 127, 127), _img(220, 220, 30)):                              # серый (нет сигнала) и жёлтый (нет в каталоге)
        result = identify(img, embedder, matcher, _lookup)
        assert result["status"] == "unknown" and result["product"] is None, img[0, 0]
    assert identify(_img(127, 127, 127), embedder, matcher, _lookup)["candidates"] != []         # запасные варианты всё равно показаны


def test_in_between_similarity_is_uncertain_with_alternatives():
    embedder, matcher = _catalog()
    orange = identify(_img(220, 120, 30), embedder, matcher, _lookup)                    # ближе к красному, но не идентичен
    assert orange["status"] == "uncertain" and orange["product"]["name"] == "Кола 0,5 л"
    assert matcher.unknown_threshold <= orange["score"] < matcher.match_threshold
    assert len(orange["candidates"]) == CANDIDATES_SHOWN and orange["candidates"][0]["id"] == "p-cola"


def test_margin_rule_makes_two_close_products_uncertain():
    embedder, matcher = _catalog(margin_threshold=0.6)
    index = matcher.index
    index.add("p-sky", embedder.embed([_img(215, 40, 40)]))                              # у «воды» появился почти красный эталон
    result = identify(_img(215, 35, 35), embedder, matcher, _lookup)
    assert result["status"] == "uncertain"                                               # без правила запаса было бы «matched»
    embedder2, matcher2 = _catalog()
    matcher2.index.add("p-sky", embedder2.embed([_img(215, 40, 40)]))
    assert identify(_img(215, 35, 35), embedder2, matcher2, _lookup)["status"] == "matched"


def test_empty_catalog_is_reported_separately():
    result = identify(_img(200, 30, 30), ColorEmbedder(), ProductMatcher(NumpyCatalogIndex()), _lookup)
    assert result == {"status": "empty_catalog", "product": None, "score": 0.0, "candidates": []}


def test_products_deleted_from_the_database_are_skipped_not_named():
    embedder, matcher = _catalog()
    only_lime = lambda ids: {i: PRODUCTS[i] for i in ids if i == "p-lime"}               # в базе остался один товар
    red = identify(_img(215, 35, 35), embedder, matcher, only_lime)
    assert red["product"] is None                                                         # название удалённого товара не показывается
    assert all(c["id"] == "p-lime" for c in red["candidates"])
    assert red["status"] == "unknown"                                                    # лайм по цвету далёк от красного
    none_left = identify(_img(215, 35, 35), embedder, matcher, lambda ids: {})
    assert none_left == {"status": "unknown", "product": None, "score": 0.0, "candidates": []}


def test_only_the_best_few_products_are_looked_up():
    embedder, matcher = _catalog()
    asked = []

    def spy(ids):
        asked.append(list(ids))
        return _lookup(ids)

    identify(_img(215, 35, 35), embedder, matcher, spy)
    assert len(asked) == 1 and len(asked[0]) == 3 and asked[0][0] == "p-cola"           # три товара в каталоге — три и спрошены


def test_add_reference_vectors_adds_to_the_index_and_saves_when_a_path_is_given():
    embedder, matcher = _catalog()
    saved = []

    class SpyIndex:
        def __init__(self, inner): self.inner = inner
        def add(self, pid, vectors): self.inner.add(pid, vectors)
        def save(self, path): saved.append(path)

    spy = SpyIndex(matcher.index)
    assert add_reference_vectors(embedder, spy, "p-sky", [_img(30, 30, 200), _img(40, 40, 230)], "models/idx") == 2
    assert saved == ["models/idx"]
    assert add_reference_vectors(embedder, spy, "p-sky", [_img(35, 35, 210)], None) == 1 and saved == ["models/idx"]
    with pytest.raises(ValueError):
        add_reference_vectors(embedder, spy, "p-sky", [], "models/idx")
    assert identify(_img(33, 33, 212), embedder, matcher, _lookup)["product"]["name"] == "Вода голубая"
