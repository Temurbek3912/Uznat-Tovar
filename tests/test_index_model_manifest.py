"""
Манифест индекса: индекс помнит модель, которой построены векторы (документ 07, §8 и §10.1; 2026-11-14).

Главные свойства:
  - модель и пространство имён сохраняются и читаются обратно (numpy и FAISS);
  - индекс другой модели (другие веса при той же размерности) не загружается — отказ с причиной, файл не трогается;
  - старый индекс без записанной модели загружается, но остаётся «непроверенным»: текущая модель ему НЕ дописывается;
  - пустой индекс получает модель, которая будет добавлять векторы;
  - перечитывание после чужой записи (refresh_index_if_changed) сверяет с той же моделью;
  - паспорт DINOv2 зависит от весов: другие файлы весов — другой отпечаток.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import embedder as E
from app.pipeline import matcher as M

MODEL_A = {"name": "dinov2-base", "weights": "hub:facebook/dinov2-base@abc", "preprocess": E.DINOV2_PREPROCESS, "dim": 3,
           "normalization": "l2"}
MODEL_B = {**MODEL_A, "name": "dinov2_base_retail_ft", "weights": "sha256:" + "0" * 64}


def _vec(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def _faiss_available() -> bool:
    try:
        M._import_faiss()
        return True
    except RuntimeError:
        return False


BACKENDS = ["npz"] + (["faiss"] if _faiss_available() else [])


def _path(tmp_path: Path, backend: str) -> str:
    return str(tmp_path / ("catalog.npz" if backend == "npz" else "catalog.faiss"))


def _build(tmp_path, backend, model=MODEL_A):
    path = _path(tmp_path, backend)
    index = M.open_catalog_index(path, 3, model)
    if backend == "npz" and not isinstance(index, M.NumpyCatalogIndex):     # файла нет: open выбирает FAISS, если он есть
        index = M.NumpyCatalogIndex()
        index.model, index.namespace = model, M.DEFAULT_NAMESPACE
    index.add("cola", _vec(1, 0, 0), ["upload:cola/1.jpg"])
    index.save(path)
    return path


@pytest.mark.parametrize("backend", BACKENDS)
def test_model_and_namespace_survive_save_and_load(tmp_path, backend):
    path = _build(tmp_path, backend)
    loaded = M.open_catalog_index(path, 3, MODEL_A)
    assert loaded.model == MODEL_A
    assert loaded.namespace == M.DEFAULT_NAMESPACE
    assert M.index_manifest(loaded) == {"model": MODEL_A, "namespace": "catalog", "count": 1, "verified": True}


@pytest.mark.parametrize("backend", BACKENDS)
def test_other_model_with_same_dimension_is_refused(tmp_path, backend):
    path = _build(tmp_path, backend)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(M.IndexIntegrityError) as err:
        M.open_catalog_index(path, 3, MODEL_B)
    text = str(err.value)
    assert "другой моделью" in text and "веса" in text and "Пересоберите" in text
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before          # файл не тронут


@pytest.mark.parametrize("backend", BACKENDS)
def test_without_model_nothing_is_checked_and_stored_model_is_kept(tmp_path, backend):
    path = _build(tmp_path, backend)
    index = M.open_catalog_index(path, 3)                                            # служебный скрипт без эмбеддера
    assert index.model == MODEL_A
    index.add("chips", _vec(0, 0, 1))
    index.save(path)
    assert M.open_catalog_index(path, 3, MODEL_A).model == MODEL_A


def _legacy_npz(path: str) -> None:
    """Индекс формата до 2026-11-14: без манифеста."""
    with open(path, "wb") as fh:
        np.savez(fh, ids=np.array(["cola"], dtype=str), vectors=np.stack([_vec(1, 0, 0)]), generation=np.array("g1"),
                 sources=np.array([""], dtype=str))


def test_legacy_index_loads_unverified_and_current_model_is_not_stamped(tmp_path, caplog):
    path = str(tmp_path / "catalog.npz")
    _legacy_npz(path)
    with caplog.at_level("WARNING"):
        index = M.open_catalog_index(path, 3, MODEL_A)
    assert index.model is None and M.index_manifest(index)["verified"] is False
    assert "до записи версии модели" in caplog.text
    index.add("chips", _vec(0, 0, 1))
    index.save(path)
    assert M.open_catalog_index(path, 3, MODEL_A).model is None                     # старые векторы — неизвестной модели


def test_empty_legacy_index_takes_current_model(tmp_path):
    path = str(tmp_path / "catalog.npz")
    with open(path, "wb") as fh:
        np.savez(fh, ids=np.array([], dtype=str), vectors=np.zeros((0, 0), dtype=np.float32), generation=np.array("g1"),
                 sources=np.array([], dtype=str))
    assert M.open_catalog_index(path, 3, MODEL_A).model == MODEL_A


def test_new_index_gets_model_of_embedder(tmp_path):
    index = M.open_catalog_index(str(tmp_path / "catalog.faiss"), 3, MODEL_A)
    assert index.model == MODEL_A and index.namespace == M.DEFAULT_NAMESPACE


def test_other_namespace_is_refused(tmp_path):
    path = _build(tmp_path, "npz")
    with pytest.raises(M.IndexIntegrityError, match="для каталога"):
        M.open_catalog_index(path, 3, MODEL_A, namespace="store-2")


def test_broken_manifest_is_corruption_not_missing_model(tmp_path):
    path = str(tmp_path / "catalog.npz")
    with open(path, "wb") as fh:
        np.savez(fh, ids=np.array(["cola"], dtype=str), vectors=np.stack([_vec(1, 0, 0)]), generation=np.array("g1"),
                 sources=np.array([""], dtype=str), manifest=np.array(json.dumps({"model": {"name": "x"}})))
    with pytest.raises(M.IndexIntegrityError):
        M.open_catalog_index(path, 3, MODEL_A)


def test_refresh_after_foreign_write_checks_same_model(tmp_path):
    path = _build(tmp_path, "npz")
    matcher = M.ProductMatcher(M.open_catalog_index(path, 3, MODEL_A))
    foreign = M.NumpyCatalogIndex()                                                  # другой процесс пересобрал индекс другой моделью
    foreign.model = MODEL_B
    foreign.add("cola", _vec(1, 0, 0))
    foreign.save(path)
    status = M.refresh_index_if_changed(matcher, path, 3)
    assert status.startswith("error") and "другой моделью" in status
    assert matcher.index.model == MODEL_A                                            # работает прежняя копия


def test_check_index_model_after_late_embedder_load(tmp_path):
    path = _build(tmp_path, "npz")
    index = M.open_catalog_index(path, 3)
    with pytest.raises(M.IndexIntegrityError):
        M.check_index_model(index, path, MODEL_B)
    M.check_index_model(index, path, MODEL_A)
    assert index._expected[0] == MODEL_A


def test_compact_keeps_manifest(tmp_path):
    from app.services import index_reconcile as R
    path = _build(tmp_path, "npz")
    index = M.open_catalog_index(path, 3, MODEL_A)
    assert R.compact(index, {"cola"}).model == MODEL_A


# --------------------------------------------------------------------------- паспорт эмбеддера

def test_debug_embedder_has_signature():
    sig = E.model_signature(E.RandomDebugEmbedder(embedding_dim=8))
    assert sig["name"] == "random-debug" and sig["dim"] == 8 and set(M.MODEL_FIELDS) <= set(sig)


def test_embedder_without_signature_gives_none():
    assert E.model_signature(object()) is None


def test_weights_fingerprint_depends_on_content_and_names(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "config.json").write_text("{}")
    (a / "model.safetensors").write_bytes(b"weights-1")
    first = E.weights_fingerprint(a)
    assert first == E.weights_fingerprint(a) and first.startswith("sha256:")
    (a / "model.safetensors").write_bytes(b"weights-2")
    assert E.weights_fingerprint(a) != first
    b = tmp_path / "b"
    b.mkdir()
    (b / "config.json").write_text("{}")
    (b / "other.safetensors").write_bytes(b"weights-1")
    assert E.weights_fingerprint(b) != first


def test_model_mismatch_names_every_difference():
    assert M.model_mismatch(MODEL_A, dict(MODEL_A)) is None
    assert M.model_mismatch(None, MODEL_A) is None
    diff = M.model_mismatch(MODEL_A, MODEL_B)
    assert "модель" in diff and "веса" in diff and "размерность" not in diff


def test_runtime_reports_index_model():
    from app.pipeline import ml_runtime as mr
    idx = M.NumpyCatalogIndex()
    idx.model = MODEL_A
    assert "dinov2-base" in mr._model_note(idx, MODEL_A)
    assert "НЕ ЗАПИСАНА" in mr._model_note(M.NumpyCatalogIndex(), MODEL_A)
