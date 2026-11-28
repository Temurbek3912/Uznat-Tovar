"""Лёгкая проекция поверх векторов модели (app/pipeline/projection.py, ml/train_projection.py; замер 2026-11-22)."""
from __future__ import annotations

import logging
import types

import numpy as np
import pytest

from app.db.models import ProductImage
from app.pipeline import embedder as E
from app.pipeline import matcher as M
from app.pipeline.embedder import model_signature, model_version
from app.pipeline.projection import PROJ_MARK, Projection, ProjectedEmbedder, with_projection
from tests.test_product_images import SIG, Embedder, _add_with_photos, _script_kw, database, env  # noqa: F401

BASE = {"name": "m", "weights": "hub:x@1", "preprocess": "p/v1", "dim": 3, "normalization": "l2"}
A = np.array([[0.0, 0.5, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, -0.5]], dtype=np.float32)


class Fake:
    embedding_dim = 3

    def __init__(self, signature=BASE):
        self.model_signature = dict(signature)
        self.device = "cpu"

    def embed(self, crops):
        return np.asarray(crops, dtype=np.float32)


def test_projection_math_signature_and_file_roundtrip(tmp_path):
    p = Projection(A, BASE, {"thresholds": [0.83, 0.08]})
    x = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32)
    want = x + x @ A
    assert np.allclose(p.apply(x), want / np.linalg.norm(want, axis=1, keepdims=True))
    assert np.allclose(np.linalg.norm(p.apply(x), axis=1), 1.0)
    assert np.allclose(Projection(np.zeros((3, 3)), BASE).apply(x), x)          # нулевая поправка — векторы как были
    sig = p.signature()
    assert sig["preprocess"].startswith("p/v1" + PROJ_MARK) and model_version(sig) != model_version(BASE)
    assert Projection(A * 2, BASE).signature() != sig                            # другая матрица — другой паспорт
    back = Projection.load(p.save(tmp_path / "p.npz"))
    assert np.array_equal(back.matrix, p.matrix) and back.signature() == sig and back.info["thresholds"] == [0.83, 0.08]
    assert p.applies_to(BASE) and not p.applies_to({**BASE, "name": "m+int8"}) and not p.applies_to(None)


def test_projection_refuses_wrong_shapes_and_projected_base():
    with pytest.raises(ValueError):
        Projection(np.zeros((3, 4)), BASE)
    with pytest.raises(ValueError):
        Projection(np.zeros((4, 4)), BASE)
    with pytest.raises(ValueError):
        Projection(A, {**BASE, "preprocess": "p/v1" + PROJ_MARK + "abc"})
    with pytest.raises(ValueError):
        Projection(A, BASE).apply(np.zeros((1, 4)))


def test_with_projection_is_off_by_default_and_safe_on_problems(tmp_path, caplog):
    base = Fake()
    assert with_projection(base, "") is base
    caplog.set_level(logging.WARNING)
    assert with_projection(base, str(tmp_path / "нет.npz")) is base                 # файла нет — без проекции
    (tmp_path / "bad.npz").write_bytes(b"not a file")
    assert with_projection(base, str(tmp_path / "bad.npz")) is base                 # испорчен — без проекции
    path = Projection(A, BASE).save(tmp_path / "p.npz")
    assert with_projection(Fake({**BASE, "name": "dinov2"}), str(path)).model_signature["name"] == "dinov2"   # другая модель
    wrapped = with_projection(base, str(path))
    assert isinstance(wrapped, ProjectedEmbedder) and wrapped.device == "cpu"
    assert PROJ_MARK in model_signature(wrapped)["preprocess"]
    x = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
    assert np.allclose(wrapped.embed(x), Projection(A, BASE).apply(x))


def test_create_embedder_applies_the_configured_projection(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "SiglipEmbedder", lambda **kw: Fake())
    path = Projection(A, BASE).save(tmp_path / "p.npz")
    cfg = types.SimpleNamespace(name="siglip2-base", weights_path="", embedding_dim=3, projection_path=str(path))
    assert isinstance(E.create_embedder(cfg), ProjectedEmbedder)
    assert isinstance(E.create_embedder(types.SimpleNamespace(name="siglip2-base", weights_path="", embedding_dim=3)), Fake)


def test_projection_is_off_in_code_and_on_in_config_with_its_thresholds():
    from app.core.config import EmbedderConfig, get_settings
    assert EmbedderConfig(name="x", weights_path="").projection_path == ""     # без строки в config.yaml — как раньше
    s = get_settings()          # 2026-11-28: дообученный SigLIP 2 и его проекция (обе — на диске, не в git), пороги 0.84 / 0.08
    assert s.embedder.weights_path == "models/embedder/siglip2-base-shop-ft"
    assert s.embedder.projection_path == "models/projection/siglip2-base-shop-ft.npz"
    assert (s.matcher.match_threshold, s.matcher.margin_threshold) == (0.84, 0.08)
    from app.core.config import resolve_project_path
    # проекция прежней модели хранится вместе с программой — чтобы вернуть как было (включена 2026-11-22, 0.83 / 0.08)
    shipped = Projection.load(resolve_project_path("models/projection/siglip2-base.npz"))
    assert shipped.base_signature["name"] == E.SIGLIP2_HUB_MODEL and shipped.dim == s.embedder.embedding_dim
    assert shipped.info["thresholds"] == [0.83, 0.08] and shipped.info["base_thresholds"] == [0.86, 0.04]


def test_retraining_compares_with_thresholds_without_projection(tmp_path):
    from ml.train_projection import base_thresholds
    path = Projection(A, BASE, {"base_thresholds": [0.86, 0.04]}).save(tmp_path / "p.npz")

    def settings(projection_path):
        return types.SimpleNamespace(embedder=types.SimpleNamespace(projection_path=projection_path),
                                     matcher=types.SimpleNamespace(match_threshold=0.83, margin_threshold=0.08))
    assert base_thresholds(settings(str(path))) == (0.86, 0.04)     # проекция включена — сравнение с порогами без неё
    assert base_thresholds(settings("")) == (0.83, 0.08)
    assert base_thresholds(settings(str(tmp_path / "нет.npz"))) == (0.83, 0.08)


def test_rebuild_with_projection_takes_model_vectors_from_cache_without_the_model(env):
    from scripts import rebuild_index
    _add_with_photos(env, "p1", "KOLA", [(200, 10, 10), (190, 20, 10)])
    _add_with_photos(env, "p2", "FANTA", [(10, 10, 200)])
    raw = M.open_catalog_index(env.path, 3, SIG)
    rows = {r.source_ref: r for r in env.db.query(ProductImage).all()}
    gone = rows[raw._sources[1]]
    env.registry.set_active(gone.id, False)                                        # выведено из эталонов
    projection = Projection(A, SIG)
    projected = ProjectedEmbedder(env.embedder, projection)
    calls = env.embedder.calls
    lines, kw = _script_kw(env, make_embedder=lambda settings: projected)
    assert rebuild_index.main(["--apply", "--yes", "--compute-missing", "--allow-missing"], **kw) == 0, "\n".join(lines)
    assert env.embedder.calls == calls                                            # фото не пересчитывались
    new = M.open_catalog_index(env.path, 3, projection.signature())
    assert new.model == projection.signature()
    got = {(pid, src): vec for pid, src, vec in zip(new._ids, new._sources, new._vectors)}
    assert len(got) == 2 and (gone.product_id, gone.source_ref) not in got
    for pid, src, vec in zip(raw._ids, raw._sources, raw._vectors):
        if src != gone.source_ref:
            assert np.allclose(got[pid, src], projection.apply(np.asarray(vec)[None])[0], atol=1e-6)
    cached = env.registry.cached_vectors(model_version(projection.signature()))
    assert len(cached) == 3 and gone.id in cached          # и выведенное фото: «Вернуть в эталоны» — из кэша, без пересчёта
    assert len(env.registry.cached_vectors(model_version(SIG))) == 3                       # векторы модели остались


# --------------------------------------------------------------------------- обучение и честная проверка


def _clusters(n_products=12, per=4, dim=16, seed=0):
    """Синтетика: у каждого товара свой центр, а фото «обратной стороны» (каждое второе) смещены общим для всех товаров
    направлением — как состав на упаковке: без проекции фото разных товаров с обратной стороны похожи друг на друга."""
    rng = np.random.default_rng(seed)
    side = rng.normal(size=dim)
    centers = rng.normal(size=(n_products, dim))
    vectors, owners = [], []
    for p in range(n_products):
        for k in range(per):
            v = centers[p] * 0.6 + (side * 1.5 if k % 2 else 0) + rng.normal(scale=0.3, size=dim)
            vectors.append(v / np.linalg.norm(v))
            owners.append(f"p{p}")
    return np.asarray(vectors, dtype=np.float32), owners


def test_trained_projection_brings_photos_of_one_product_together():
    from ml.train_projection import project, train_matrix
    vectors, owners = _clusters()
    owners = np.asarray(owners)
    same = owners[:, None] == owners[None, :]
    off = ~np.eye(len(owners), dtype=bool)

    def gap(z):
        s = z @ z.T
        return s[same & off].mean() - s[~same].mean()
    z = project(vectors, train_matrix(vectors, owners, {"epochs": 100, "lr": 1e-2}))
    assert gap(z) > gap(vectors) + 0.1


def test_check_splits_by_products_and_finds_copies_by_raw_vectors():
    from ml.train_projection import product_parts, ranked_lists
    _, owners = _clusters()
    parts = product_parts(owners, 5)
    assert set().union(*parts) == set(owners) and sum(len(p) for p in parts) == len(set(owners))
    assert all(not (a & b) for i, a in enumerate(parts) for b in parts[i + 1:])
    raw = np.array([[1, 0], [1, 0], [0, 1]], dtype=np.float32)     # фото 0 и 1 — копии одного снимка
    z = np.array([[1, 0], [0.6, 0.8], [0, 1]], dtype=np.float32)   # после проекции они уже «не копии»
    owners = np.array(["a", "a", "b"])
    assert 0 not in ranked_lists(z, raw, owners, [0])              # копия убрана по исходным векторам — снимка нет
    assert ranked_lists(z, raw, owners, [0], stranger=True)[0][0][0] == "b"


def test_skip_check_writes_projection_without_cross_check(tmp_path, monkeypatch):
    """--skip-check (дообученная модель видела все товары — её проверка в ml.finetune_check): файл пишется без проверки."""
    from ml import train_projection as tp
    vectors, owners = _clusters()
    lines = []
    settings = types.SimpleNamespace(embedder=types.SimpleNamespace(projection_path=""),
                                     matcher=types.SimpleNamespace(match_threshold=0.84, margin_threshold=0.08,
                                                                   unknown_threshold=0.70))
    sig = {**BASE, "dim": vectors.shape[1]}
    monkeypatch.setattr(tp, "load_catalog_vectors", lambda *a, **k: (vectors, owners, [True] * len(owners), sig, 0))
    monkeypatch.setattr(tp, "cross_check", lambda *a, **k: pytest.fail("проверки быть не должно"))
    assert tp.main(["--skip-check", "--out", str(tmp_path / "p.npz")], settings=settings, out=lines.append) == 0
    saved = Projection.load(tmp_path / "p.npz")
    assert saved.base_signature == sig and saved.info["thresholds"] is None and saved.info["base_thresholds"] is None
    assert (tmp_path / "p.md").exists()
