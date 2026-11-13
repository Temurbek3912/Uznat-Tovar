"""
app/pipeline/matcher.py::rank_products / decide_match / ProductMatcher с правилом запаса (margin).
Реально выполняется (numpy). decide_match — единственная реализация решения: тот же код вызывают оценка и подбор порогов.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from app.core.schemas import DetectionStatus as S
from app.pipeline.matcher import NumpyCatalogIndex, ProductMatcher, decide_match, rank_products

ROOT = Path(__file__).resolve().parent.parent


def _unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


# ----------------------------------------------------------------------- группировка по товару

def test_rank_products_keeps_the_best_score_per_product_in_descending_order():
    ranked = rank_products([("a", 0.90), ("a", 0.95), ("b", 0.70), ("a", 0.80), ("c", 0.70), ("b", 0.75)])
    assert ranked == [("a", 0.95), ("b", 0.75), ("c", 0.70)]
    assert rank_products([]) == []


def test_rank_products_ties_keep_first_seen_order():
    assert [p for p, _ in rank_products([("x", 0.5), ("y", 0.5), ("z", 0.5)])] == ["x", "y", "z"]


# ----------------------------------------------------------------------- решение

def test_empty_ranking_is_unknown_with_zero_score():
    r = decide_match([], 0.72, 0.45)
    assert (r.product_id, r.score, r.status) == (None, 0.0, S.UNKNOWN)


def test_basic_three_way_decision_with_inclusive_boundaries():
    assert decide_match([("a", 0.80)], 0.72, 0.45).status == S.MATCHED
    assert decide_match([("a", 0.72)], 0.72, 0.45).status == S.MATCHED          # ровно порог — уже «товар»
    uncertain = decide_match([("a", 0.60)], 0.72, 0.45)
    assert (uncertain.status, uncertain.product_id) == (S.UNCERTAIN, "a")        # кандидат сохраняется для ревью
    assert decide_match([("a", 0.45)], 0.72, 0.45).status == S.UNCERTAIN         # ровно нижний порог — ещё «не уверен»
    unknown = decide_match([("a", 0.30)], 0.72, 0.45)
    assert (unknown.status, unknown.product_id, unknown.score) == (S.UNKNOWN, None, 0.30)


def test_margin_rule_is_off_by_default_and_with_zero():
    close = [("a", 0.90), ("b", 0.899)]
    assert decide_match(close, 0.72, 0.45).status == S.MATCHED
    assert decide_match(close, 0.72, 0.45, margin_threshold=0.0).status == S.MATCHED


def test_close_runner_up_turns_a_confident_match_into_uncertain_but_keeps_the_candidate():
    r = decide_match([("a", 0.90), ("b", 0.88)], 0.72, 0.45, margin_threshold=0.05)
    assert (r.status, r.product_id, r.score) == (S.UNCERTAIN, "a", 0.90)
    assert (r.runner_up_id, r.runner_up_score) == ("b", 0.88)
    assert decide_match([("a", 0.90), ("b", 0.80)], 0.72, 0.45, margin_threshold=0.05).status == S.MATCHED


def test_margin_boundary_is_inclusive_and_a_lone_product_is_never_crowded():
    # отрыв ровно равен порогу — достаточно (>=); значения подобраны точно представимыми в float
    assert decide_match([("a", 0.75), ("b", 0.50)], 0.72, 0.45, margin_threshold=0.25).status == S.MATCHED
    assert decide_match([("a", 0.75), ("b", 0.51)], 0.72, 0.45, margin_threshold=0.25).status == S.UNCERTAIN
    only = decide_match([("a", 0.95)], 0.72, 0.45, margin_threshold=0.5)
    assert only.status == S.MATCHED and only.runner_up_id is None


def test_margin_does_not_touch_decisions_that_are_already_uncertain_or_unknown():
    assert decide_match([("a", 0.60), ("b", 0.10)], 0.72, 0.45, margin_threshold=0.5).status == S.UNCERTAIN
    assert decide_match([("a", 0.30), ("b", 0.10)], 0.72, 0.45, margin_threshold=0.5).status == S.UNKNOWN


# ----------------------------------------------------------------------- ProductMatcher + индекс

def _index_with_confusable_skus():
    """SKU A — 6 эталонных фото рядом с осью X; SKU B — один вектор почти там же (похожий вкус того же бренда)."""
    index = NumpyCatalogIndex()
    for dy in (0.00, 0.01, 0.02, 0.03, 0.04, 0.05):
        index.add("A", _unit(1.0, dy, 0.0))
    index.add("B", _unit(1.0, 0.12, 0.0))
    return index


def test_matcher_finds_the_rival_product_even_when_the_top_vectors_all_belong_to_one_sku():
    index = _index_with_confusable_skus()
    query = _unit(1.0, 0.06, 0.0)
    wide = ProductMatcher(index, top_k=5, match_threshold=0.9, unknown_threshold=0.5, margin_threshold=0.02, candidates_k=25)
    r = wide.match(query)
    assert r.status == S.UNCERTAIN and r.product_id == "A" and r.runner_up_id == "B"
    # узкий просмотр: все 5 ближайших векторов — товар A, соперника не видно -> «уверенный» матч (то, ради чего нужен candidates_k)
    narrow = ProductMatcher(index, top_k=5, match_threshold=0.9, unknown_threshold=0.5, margin_threshold=0.02, candidates_k=5)
    assert narrow.match(query).status == S.MATCHED


def test_matcher_with_default_margin_behaves_exactly_like_before():
    index = _index_with_confusable_skus()
    assert ProductMatcher(index, top_k=5, match_threshold=0.9, unknown_threshold=0.5).match(_unit(1.0, 0.06, 0.0)).status == S.MATCHED
    empty = ProductMatcher(NumpyCatalogIndex()).match(_unit(1, 0, 0))
    assert empty.status == S.UNKNOWN and empty.product_id is None


def test_match_batch_applies_the_rule_per_vector():
    index = _index_with_confusable_skus()
    index.add("C", _unit(0.0, 0.0, 1.0))
    matcher = ProductMatcher(index, top_k=5, match_threshold=0.9, unknown_threshold=0.5, margin_threshold=0.02)
    results = matcher.match_batch(np.stack([_unit(1.0, 0.06, 0.0), _unit(0.0, 0.0, 1.0)]))
    assert [r.status for r in results] == [S.UNCERTAIN, S.MATCHED] and results[1].product_id == "C"


# ----------------------------------------------------------------------- конфигурация и воркер

def test_margin_is_configurable_off_by_default_and_passed_by_the_worker():
    assert "margin_threshold: 0.0" in (ROOT / "configs" / "config.yaml").read_text(encoding="utf-8")
    assert "margin_threshold: float = 0.0" in (ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")
    runtime = (ROOT / "app" / "pipeline" / "ml_runtime.py").read_text(encoding="utf-8")        # матчер строится в ml_runtime
    assert "margin_threshold=cfg.margin_threshold if cfg else 0.0" in runtime
