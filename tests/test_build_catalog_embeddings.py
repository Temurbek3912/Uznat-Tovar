"""
Опытный индекс из папки (ml/build_catalog_embeddings.py, 2026-11-21): папки сопоставляются с товарами базы (id или артикул),
неизвестные — отказ до загрузки модели и до записи; рабочий и облегчённый индексы инструмент не пишет.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.config import PROJECT_ROOT
from app.pipeline import matcher as M
from ml import build_catalog_embeddings as B
from tests.test_index_integrity import _make_fake_faiss

PRODUCTS = {"p1": "KOLA-0.5", "p2": "FANTA-0.5"}
SIGNATURE = {"name": "fake", "weights": "w", "preprocess": "p", "dim": 3, "normalization": "l2"}


@pytest.fixture(autouse=True)
def fake_faiss(monkeypatch):
    monkeypatch.setitem(sys.modules, "faiss", _make_fake_faiss())


class FakeEmbedder:
    model_signature = SIGNATURE

    def embed(self, images):
        return np.array([[1.0, 0.0, 0.0]] * len(images), dtype=np.float32)


def _settings(tmp_path: Path):
    return SimpleNamespace(embedder=SimpleNamespace(embedding_dim=3),
                           matcher=SimpleNamespace(index_path=str(tmp_path / "work" / "catalog.faiss")))


def _catalog(tmp_path: Path, folders) -> Path:
    root = tmp_path / "catalog"
    for name in folders:
        (root / name).mkdir(parents=True)
        (root / name / "1.jpg").write_bytes(b"x")
        (root / name / "2.png").write_bytes(b"y")
    return root


def _run(tmp_path, argv, loaded: list):
    lines: list[str] = []

    def make_embedder(cfg):
        loaded.append(cfg)
        return FakeEmbedder()

    code = B.main(argv, out=lines.append, settings=_settings(tmp_path), load_products=lambda: dict(PRODUCTS),
                  make_embedder=make_embedder, load_image=lambda p: p)
    return code, "\n".join(lines)


def test_unknown_folders_are_refused_before_the_model_and_before_any_write(tmp_path):
    catalog = _catalog(tmp_path, ["p1", "SPRITE-1L", "WATER"])
    out_dir = tmp_path / "exp"
    loaded: list = []
    code, text = _run(tmp_path, ["--catalog-dir", str(catalog), "--output-index", str(out_dir / "catalog.faiss")], loaded)
    assert code == 1 and loaded == []
    assert "неизвестная папка: SPRITE-1L" in text and "неизвестная папка: WATER" in text and "2 из 3" in text
    assert "scripts.import_catalog" in text and "Ничего не записано" in text
    assert not out_dir.exists()


def test_folders_named_by_sku_are_mapped_to_product_ids(tmp_path):
    catalog = _catalog(tmp_path, ["KOLA-0.5", "p2"])
    out = tmp_path / "exp" / "catalog.faiss"
    code, text = _run(tmp_path, ["--catalog-dir", str(catalog), "--output-index", str(out)], [])
    assert code == 0, text
    assert "по артикулу: 1" in text
    index = M.open_catalog_index(str(out), 3)
    assert index._ids == ["p1", "p1", "p2", "p2"]                              # id товаров базы, а не артикулы
    assert list(index._sources) == ["file:KOLA-0.5/1.jpg", "file:KOLA-0.5/2.png", "file:p2/1.jpg", "file:p2/2.png"]
    assert index.model == SIGNATURE
    manifest = json.loads(Path(f"{out}.build_manifest.json").read_text(encoding="utf-8"))
    assert manifest == {"p1": 2, "p2": 2}
    assert not (tmp_path / "work").exists()                                    # рабочая папка индекса не тронута


@pytest.mark.parametrize("target", ["work", "lite", "none"])
def test_working_and_lite_index_are_never_written(tmp_path, target):
    catalog = _catalog(tmp_path, ["p1"])
    argv = ["--catalog-dir", str(catalog)]
    if target == "work":
        argv += ["--output-index", str(tmp_path / "work" / "catalog.faiss"), "--replace-existing"]
    elif target == "lite":
        argv += ["--output-index", str(PROJECT_ROOT / "data" / "lite" / "index" / "catalog.faiss"), "--replace-existing"]
    loaded: list = []
    code, text = _run(tmp_path, argv, loaded)
    assert code == 2 and loaded == []
    assert "scripts.rebuild_index" in text and "scripts.import_catalog" in text
    assert not (tmp_path / "work").exists()


def test_existing_experimental_index_needs_replace_existing_and_is_copied_first(tmp_path):
    catalog = _catalog(tmp_path, ["p1"])
    out = tmp_path / "exp" / "catalog.faiss"
    assert _run(tmp_path, ["--catalog-dir", str(catalog), "--output-index", str(out)], [])[0] == 0
    loaded: list = []
    code, text = _run(tmp_path, ["--catalog-dir", str(catalog), "--output-index", str(out)], loaded)
    assert code == 1 and loaded == [] and "replace-existing" in text
    code, text = _run(tmp_path, ["--catalog-dir", str(catalog), "--output-index", str(out), "--replace-existing"], [])
    assert code == 0 and "Копия прежнего индекса" in text
    assert any(p.name.startswith("backup-") for p in out.parent.iterdir())
