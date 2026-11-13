"""
Ядро распознавания: ProductMatcher над индексом отделяет matched / uncertain / unknown по порогам и честно отвечает unknown,
когда индекс пуст. Перенесено без изменений из tests/test_pipeline.py (конвейер аудита полок удалён, а эти проверки нужны «Узнать товар»).

Намеренно без настоящих весов DINOv2: векторы заданы вручную, поэтому тест быстрый и детерминированный.
"""
from __future__ import annotations

import numpy as np

from app.core.schemas import DetectionStatus
from app.pipeline.matcher import NumpyCatalogIndex, ProductMatcher


def _l2_normalize(v: list[float]) -> np.ndarray:
    arr = np.array(v, dtype=np.float32)
    return arr / np.linalg.norm(arr)


class TestMatcher:
    def test_matched_above_threshold(self):
        index = NumpyCatalogIndex()
        index.add("cola_05", _l2_normalize([1, 0, 0]))
        index.add("chips", _l2_normalize([0, 0, 1]))
        matcher = ProductMatcher(index, top_k=2, match_threshold=0.9, unknown_threshold=0.4)

        result = matcher.match(_l2_normalize([1, 0.02, 0]))

        assert result.status == DetectionStatus.MATCHED
        assert result.product_id == "cola_05"

    def test_uncertain_between_thresholds(self):
        index = NumpyCatalogIndex()
        index.add("cola_05", _l2_normalize([1, 0, 0]))
        matcher = ProductMatcher(index, top_k=1, match_threshold=0.95, unknown_threshold=0.5)

        result = matcher.match(_l2_normalize([1, 1, 0]))  # ~0.707 similarity

        assert result.status == DetectionStatus.UNCERTAIN

    def test_unknown_when_no_close_match(self):
        index = NumpyCatalogIndex()
        index.add("cola_05", _l2_normalize([1, 0, 0]))
        matcher = ProductMatcher(index, top_k=1, match_threshold=0.9, unknown_threshold=0.6)

        result = matcher.match(_l2_normalize([0, 1, 0]))  # ортогонально, similarity ~0

        assert result.status == DetectionStatus.UNKNOWN
        assert result.product_id is None

    def test_empty_index_returns_unknown(self):
        matcher = ProductMatcher(NumpyCatalogIndex(), top_k=5)
        result = matcher.match(_l2_normalize([1, 0, 0]))
        assert result.status == DetectionStatus.UNKNOWN
