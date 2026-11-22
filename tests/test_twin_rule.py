"""
Правило двойника и строгий режим (app/pipeline/matcher.decide_match / ProductMatcher.twin_similarity, app/pipeline/ml_runtime,
замер 2026-11-23 ml/check_twins.py: уверенных ошибок 23 -> 2 из 1290 снимков каталога). Реально выполняется (numpy).

Двойник — другой товар, чьи фото в каталоге почти такие же, как у лучшего (вкус или объём той же линейки). Снимок может быть
намного ближе к «чужому» близнецу, и правило запаса этого не видит: у второго товара счёт низкий. Такой ответ — «похоже».
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.core.schemas import DetectionStatus as S
from app.pipeline.matcher import NumpyCatalogIndex, ProductMatcher, decide_match

ROOT = Path(__file__).resolve().parent.parent


def _unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def _twins(table):
    return lambda a, b: table.get((a, b), table.get((b, a), -1.0))


# ----------------------------------------------------------------------- решение

def test_a_confident_match_with_a_twin_among_candidates_becomes_uncertain_even_with_a_big_gap():
    ranked = [("bio_c", 0.85), ("yogurt", 0.70), ("bio_c_peach", 0.68)]
    twins = _twins({("bio_c", "bio_c_peach"): 0.82})
    r = decide_match(ranked, 0.83, 0.70, 0.08, 0.80, twins)
    assert r.status == S.UNCERTAIN and r.product_id == "bio_c"
    assert (r.runner_up_id, r.runner_up_score) == ("bio_c_peach", 0.68)     # человек увидит близнеца среди вариантов


def test_without_a_twin_or_with_the_rule_off_the_match_stays_confident():
    ranked = [("bio_c", 0.85), ("yogurt", 0.70), ("bio_c_peach", 0.68)]
    far = _twins({("bio_c", "bio_c_peach"): 0.60})
    assert decide_match(ranked, 0.83, 0.70, 0.08, 0.80, far).status == S.MATCHED
    near = _twins({("bio_c", "bio_c_peach"): 0.95})
    assert decide_match(ranked, 0.83, 0.70, 0.08, 0.0, near).status == S.MATCHED        # 0 — выключено (как раньше)
    assert decide_match(ranked, 0.83, 0.70, 0.08, 0.80, None).status == S.MATCHED       # нечем сравнить — как раньше
    assert decide_match(ranked, 0.83, 0.70, 0.08).status == S.MATCHED                   # старый вызов не изменился


def test_twin_rule_never_raises_uncertain_or_unknown_to_matched():
    twins = _twins({("a", "b"): 0.99})
    assert decide_match([("a", 0.75), ("b", 0.5)], 0.83, 0.70, 0.08, 0.80, twins).status == S.UNCERTAIN
    assert decide_match([("a", 0.50), ("b", 0.4)], 0.83, 0.70, 0.08, 0.80, twins).status == S.UNKNOWN


def test_twin_boundary_is_inclusive():
    ranked = [("a", 0.9), ("b", 0.6)]
    assert decide_match(ranked, 0.83, 0.7, 0.08, 0.80, _twins({("a", "b"): 0.80})).status == S.UNCERTAIN
    assert decide_match(ranked, 0.83, 0.7, 0.08, 0.80, _twins({("a", "b"): 0.7999})).status == S.MATCHED


# ----------------------------------------------------------------------- матчер: двойник по фото каталога

def _catalog():
    index = NumpyCatalogIndex()
    index.add("bio_c", np.stack([_unit(1.0, 0.0, 0.0), _unit(1.0, 0.3, 0.0)]), ["upload:bio_c/1.jpg", "upload:bio_c/2.jpg"])
    index.add("bio_c_peach", np.stack([_unit(1.0, 0.35, 0.05), _unit(0.0, 1.0, 1.0)]),     # первое фото — почти как у bio_c
              ["upload:peach/1.jpg", "upload:peach/2.jpg"])
    index.add("cola", _unit(0.0, 0.0, 1.0)[None], ["upload:cola/1.jpg"])
    return index


def test_twin_similarity_is_the_best_photo_pair_and_ignores_photos_taken_out_of_references():
    matcher = ProductMatcher(_catalog(), twin_threshold=0.80)
    expected = float(_unit(1.0, 0.3, 0.0) @ _unit(1.0, 0.35, 0.05))
    assert matcher.twin_similarity("bio_c", "bio_c_peach") == pytest.approx(expected)
    assert matcher.twin_similarity("bio_c_peach", "bio_c") == pytest.approx(expected)
    assert matcher.twin_similarity("bio_c", "cola") < 0.5
    matcher.excluded = frozenset({("bio_c_peach", "upload:peach/1.jpg")})               # фото выведено из эталонов
    assert matcher.twin_similarity("bio_c", "bio_c_peach") < 0.8
    assert matcher.twin_similarity("bio_c", "nope") == -1.0


def test_matcher_turns_a_match_into_uncertain_when_the_best_product_has_a_twin_in_the_catalog():
    query = _unit(1.0, 0.02, 0.0)                     # почти точно фото bio_c; bio_c_peach далеко по счёту, но близнец по каталогу
    off = ProductMatcher(_catalog(), match_threshold=0.9, unknown_threshold=0.3, margin_threshold=0.02)
    on = ProductMatcher(_catalog(), match_threshold=0.9, unknown_threshold=0.3, margin_threshold=0.02, twin_threshold=0.95)
    assert off.match(query).status == S.MATCHED
    r = on.match(query)
    assert r.status == S.UNCERTAIN and r.product_id == "bio_c" and r.runner_up_id == "bio_c_peach"
    lone = ProductMatcher(_catalog(), match_threshold=0.9, unknown_threshold=0.3, twin_threshold=0.95)
    assert lone.match(_unit(0.0, 0.0, 1.0)).status == S.MATCHED                       # у колы двойника нет


def test_twin_cache_follows_index_changes():
    index = _catalog()
    matcher = ProductMatcher(index, twin_threshold=0.80)
    assert matcher.twin_similarity("bio_c", "cola") < 0.5
    index.add("cola", _unit(1.0, 0.0, 0.01)[None], ["upload:cola/2.jpg"])             # новое фото колы — как у bio_c
    assert matcher.twin_similarity("bio_c", "cola") > 0.99


def test_faiss_index_returns_product_vectors_too():
    pytest.importorskip("faiss")
    from app.pipeline.matcher import FaissCatalogIndex
    index = FaissCatalogIndex(3)
    index.add("a", np.stack([_unit(1, 0, 0), _unit(0, 1, 0)]), ["s1", "s2"])
    index.add("b", _unit(0, 0, 1)[None], ["s3"])
    vectors, sources = index.product_vectors("a")
    assert vectors.shape == (2, 3) and sources == ["s1", "s2"]
    assert np.allclose(vectors[1], _unit(0, 1, 0))
    assert index.product_vectors("x")[0].shape == (0, 3)
    assert ProductMatcher(index).twin_similarity("a", "b") == pytest.approx(0.0, abs=1e-6)


# ----------------------------------------------------------------------- «Узнать товар» и настройки

def test_identify_uses_the_twin_rule():
    from app.services.identify_service import ProductInfo, identify

    class Embedder:
        def embed(self, images):
            return np.stack([_unit(1.0, 0.02, 0.0)])

    def lookup(ids):
        return {pid: ProductInfo(pid, pid) for pid in ids}

    on = ProductMatcher(_catalog(), match_threshold=0.9, unknown_threshold=0.3, twin_threshold=0.95)
    off = ProductMatcher(_catalog(), match_threshold=0.9, unknown_threshold=0.3)
    image = np.zeros((4, 4, 3), np.uint8)
    assert identify(image, Embedder(), off, lookup)["status"] == "matched"
    result = identify(image, Embedder(), on, lookup)
    assert result["status"] == "uncertain" and result["product"]["id"] == "bio_c"
    assert "bio_c_peach" in [c["id"] for c in result["candidates"]]          # близнец — среди вариантов на выбор


def test_strict_mode_raises_only_the_match_threshold():
    from types import SimpleNamespace

    from app.pipeline.ml_runtime import effective_match_threshold
    cfg = SimpleNamespace(match_threshold=0.83, strict=False, strict_match_threshold=0.88)
    assert effective_match_threshold(cfg) == 0.83
    cfg.strict = True
    assert effective_match_threshold(cfg) == 0.88
    cfg.match_threshold = 0.9                                         # строгий режим никогда не ослабляет порог
    assert effective_match_threshold(cfg) == 0.9
    assert effective_match_threshold(None) == 0.72


def test_config_turns_the_twin_rule_on_keeps_strict_mode_off_and_code_defaults_are_the_old_behaviour():
    import yaml
    models = yaml.safe_load((ROOT / "configs" / "config.yaml").read_text(encoding="utf-8"))["models"]
    cfg = models["matcher"]
    if models["embedder"].get("projection_path"):                     # значение подобрано для SigLIP 2 с проекцией
        assert cfg["twin_threshold"] == 0.80
    assert cfg["strict"] is False and cfg["strict_match_threshold"] == 0.88
    from app.core.config import MatcherConfig
    default = MatcherConfig(index_path="x")
    assert default.twin_threshold == 0.0 and default.strict is False


def test_worker_matcher_gets_the_twin_threshold_and_strict_threshold():
    from types import SimpleNamespace

    from app.pipeline.ml_runtime import default_factories
    cfg = SimpleNamespace(top_k=5, match_threshold=0.83, unknown_threshold=0.7, margin_threshold=0.08, twin_threshold=0.8,
                          strict=True, strict_match_threshold=0.88)
    matcher = default_factories().matcher(NumpyCatalogIndex(), cfg)
    assert (matcher.match_threshold, matcher.twin_threshold, matcher.margin_threshold) == (0.88, 0.8, 0.08)
