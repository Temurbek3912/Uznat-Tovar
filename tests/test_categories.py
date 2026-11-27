"""Единый список категорий и категория нового товара сама (задание владельца 2026-11-27)."""
from __future__ import annotations

import importlib
import json
import re
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from app.pipeline.kind_classifier import NONE, scores_from_logits
from app.services import categories as cats
from app.services.category_guess import guess_category, neighbor_vote, neighbors_in_index, product_neighbors
from app.services.product_kinds import KIND_IDS

FRONT = Path(__file__).resolve().parents[1] / "frontend"


def test_every_kind_has_one_category_and_back():
    assert sorted(c.kind for c in cats.CATEGORIES) == sorted(KIND_IDS)
    assert len({c.ru for c in cats.CATEGORIES}) == len(cats.CATEGORIES)
    assert cats.category_for_kind("sweets") == "Сладости" and cats.kind_of_category(" Сладости ") == "sweets"
    assert cats.category_for_kind("nope") == "" and cats.kind_of_category("Моя полка") == ""
    assert not cats.is_known(cats.DEFAULT_CATEGORY) and not cats.is_known("  ") and cats.is_known("Моя полка")


def test_labels_in_four_languages():
    assert cats.label("Снеки", "uz") == "Gazaklar"
    assert cats.label("Снеки", "uz-Cyrl") == "Газаклар"
    assert cats.label("Снеки", "en") == "Snacks"
    assert cats.label("Снеки", "ru") == "Снеки"
    assert cats.label("Моя полка", "en") == "Моя полка"


def _dict(lang: str) -> dict[str, str]:
    text = (FRONT / f"i18n.{lang}.js").read_text(encoding="utf-8")
    return dict(re.findall(r'"(category\.[a-z_]+)": "([^"]*)"', text))


@pytest.mark.parametrize("lang,attr", [("ru", "ru"), ("uz", "uz"), ("uz-cyrl", "uz_cyrl"), ("en", "en")])
def test_page_dictionaries_match_the_list(lang, attr):
    assert _dict(lang) == {f"category.{c.kind}": getattr(c, attr) for c in cats.CATEGORIES}


def test_page_table_of_russian_names_matches_the_list():
    text = (FRONT / "i18n.js").read_text(encoding="utf-8")
    table = json.loads(re.search(r"var CATEGORY_KINDS = (\{.*?\});", text).group(1))
    assert table == cats.KIND_BY_RU


def test_scores_keep_every_kind_probability():
    groups = ["drink", "drink", "sweets", NONE]
    score = scores_from_logits(groups, np.array([1.0, 3.0, 2.0, 0.0]))
    assert score.kind == "drink"
    assert set(score.probs) == {"drink", "sweets", NONE}
    assert sum(score.probs.values()) == pytest.approx(1.0)


def test_neighbors_one_per_product_without_itself_and_archived():
    hits = [("self", 0.99), ("a", 0.8), ("a", 0.9), ("b", 0.85), ("gone", 0.95)]
    categories = {"self": "Без категории", "a": "Сладости", "b": "Снеки"}
    assert product_neighbors(hits, categories, exclude="self") == [("Сладости", 0.9), ("Снеки", 0.85)]


def test_vote_skips_far_and_uncategorized_neighbors():
    nb = [("Без категории", 0.95), ("Сладости", 0.9), ("Сладости", 0.8), ("Снеки", 0.75), ("Напитки", 0.5)]
    winner, share = neighbor_vote(nb)
    assert winner == "Сладости" and share == pytest.approx(1.7 / 2.45)
    assert neighbor_vote([("Напитки", 0.3)]) is None


def test_name_neighbors_and_model_add_up():
    nb = [("Сладости", 0.9), ("Сладости", 0.85)]
    guess = guess_category("Батончик Snickers", nb, {"sweets": 0.7, "snack": 0.3})
    assert guess.category == "Сладости" and guess.confidence == pytest.approx((1 + 1 + 1.5 * 0.7) / 3.5, abs=1e-3)
    # название ничего не говорит, соседи и модель спорят: побеждает большая сумма
    guess = guess_category("Ringo", [("Снеки", 0.8)], {"sweets": 0.9, "snack": 0.1})
    assert guess.category == "Сладости" and guess.source == "kind_model"


def test_unsure_stays_without_category():
    assert guess_category("Желание", [], None) is None
    assert guess_category("Желание", [], {k: 1 / len(KIND_IDS) for k in KIND_IDS}) is None
    assert guess_category("Желание", [], {"tea_coffee": 0.5, "drink": 0.5}).category == "Чай и кофе"   # 0,75/3,5 ≥ 0,2


def test_without_model_its_weight_is_not_counted():
    guess = guess_category("Майонез провансаль", [], None)
    assert guess.category == "Соусы и специи" and guess.confidence == pytest.approx(0.5)


class _Index:
    def __init__(self):
        self.rows = [("new", np.array([1.0, 0.0])), ("a", np.array([0.9, 0.1])), ("b", np.array([0.0, 1.0]))]

    def product_vectors(self, pid):
        return np.array([v for p, v in self.rows if p == pid]), [None]

    def search(self, query, top_k):
        scored = sorted(((p, float(v @ query)) for p, v in self.rows), key=lambda x: -x[1])
        return scored[:top_k]


def test_neighbors_in_index():
    assert neighbors_in_index(_Index(), "new", {"new": "", "a": "Сладости", "b": "Снеки"}) == [("Сладости", 0.9), ("Снеки", 0.0)]


class _Product:
    def __init__(self, category):
        self.id, self.sku, self.name, self.category, self.version, self.updated_at = "new", "SKU", "Ringo", category, 1, None


class _Db:
    def __init__(self, product):
        self.product, self.commits, self.rollbacks = product, 0, 0

    def get(self, _model, _pid):
        return self.product

    def query(self, *_cols):
        rows = [types.SimpleNamespace(id="new", category=self.product.category),
                types.SimpleNamespace(id="a", category="Снеки")]
        return types.SimpleNamespace(filter=lambda *_a: types.SimpleNamespace(all=lambda: rows))

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture()
def worker(monkeypatch):
    fake = sys.modules.pop("app.worker", None)
    try:
        module = importlib.import_module("app.worker")
        monkeypatch.setattr(module, "_matcher", types.SimpleNamespace(index=_Index()))
        monkeypatch.setattr(module.settings, "naming_kind_model", "", raising=False)
        yield module
    finally:
        if fake is not None:
            sys.modules["app.worker"] = fake


def test_new_product_without_category_gets_one(worker):
    db = _Db(_Product("Без категории"))
    worker._auto_category(db, "new", None)
    assert db.product.category == "Снеки" and db.product.version == 2 and db.commits == 1


def test_category_chosen_by_owner_is_kept(worker):
    db = _Db(_Product("Моя полка"))
    worker._auto_category(db, "new", None)
    assert db.product.category == "Моя полка" and db.commits == 0


def test_failure_does_not_break_adding(worker, monkeypatch):
    monkeypatch.setattr(worker, "_matcher", None)
    db = _Db(_Product("Без категории"))
    worker._auto_category(db, "new", None)
    assert db.product.category == "Без категории" and db.rollbacks == 1
