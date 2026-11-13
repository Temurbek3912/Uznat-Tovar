"""
Проверка save()/load() для NumpyCatalogIndex — round-trip сериализации
должен давать идентичные результаты поиска до и после. FaissCatalogIndex
не тестируется здесь напрямую (требует faiss-cpu, которого нет в CI без
GPU-профиля), но использует тот же паттерн save/load — см. matcher.py.

Примечание: этот файл, в отличие от test_privacy.py/test_rectify.py,
импортирует app.core.schemas и потому требует pydantic из requirements.txt.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.pipeline.matcher import NumpyCatalogIndex, ProductMatcher


def _l2n(v: list[float]) -> np.ndarray:
    arr = np.array(v, dtype=np.float32)
    return arr / np.linalg.norm(arr)


class TestNumpyCatalogIndexPersistence:
    def _build_index(self) -> NumpyCatalogIndex:
        index = NumpyCatalogIndex()
        index.add("cola_05", _l2n([1, 0, 0]))
        index.add("cola_15", _l2n([0.9, 0.1, 0]))
        index.add("chips", _l2n([0, 0, 1]))
        return index

    def test_save_creates_npz_in_nested_dir(self, tmp_path):
        index_path = tmp_path / "sub" / "dir" / "catalog.faiss"  # намеренно без .npz
        self._build_index().save(str(index_path))

        assert (tmp_path / "sub" / "dir" / "catalog.faiss.npz").exists()

    def test_load_roundtrip_preserves_search_results(self, tmp_path):
        index_path = tmp_path / "catalog"
        original = self._build_index()
        original.save(str(index_path))

        loaded = NumpyCatalogIndex.load(str(index_path))
        query = _l2n([1, 0.05, 0])

        assert loaded.search(query, top_k=3) == original.search(query, top_k=3)

    def test_loaded_index_works_with_product_matcher(self, tmp_path):
        index_path = tmp_path / "catalog"
        self._build_index().save(str(index_path))

        loaded = NumpyCatalogIndex.load(str(index_path))
        matcher = ProductMatcher(loaded, top_k=1, match_threshold=0.9, unknown_threshold=0.5)

        result = matcher.match(_l2n([1, 0.02, 0]))
        assert result.product_id == "cola_05"

    def test_save_and_load_empty_index(self, tmp_path):
        index_path = tmp_path / "empty"
        NumpyCatalogIndex().save(str(index_path))

        loaded = NumpyCatalogIndex.load(str(index_path))
        assert loaded.search(_l2n([1, 0, 0])) == []

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            NumpyCatalogIndex.load(str(tmp_path / "does_not_exist"))
