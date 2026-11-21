"""Векторы выведенных из эталонов фото убираются из файла индекса, вернувшихся — возвращаются из кэша базы
(app/services/index_prune.py, 2026-11-21): правила плана, сохранение под сверкой поколения, оба вида индекса, восстановление
без повторного добавления убранного вектора, отчёт полноты без ложных «фото без вектора»."""
from __future__ import annotations

import types
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import matcher as M
from app.pipeline.index_store import IndexChangedError
from app.services import index_coverage as C
from app.services import index_prune as X
from app.services.catalog_upload import folder_name, list_photo_files, upload_ref

SIG = {"name": "t", "weights": "w", "preprocess": "p", "dim": 3, "normalization": "l2"}
NOW = datetime(2026, 11, 21, 12, 0)
LONG_AGO = NOW - timedelta(days=X.PRUNE_AFTER_DAYS + 1)
RECENTLY = NOW - timedelta(days=X.PRUNE_AFTER_DAYS - 1)


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def img(image_id, pid, source, *, active=True, role="reference", deactivated_at=None):
    return types.SimpleNamespace(id=image_id, product_id=pid, source_ref=source, role=role, is_active=active,
                                 deactivated_at=deactivated_at if not active else None)


VEC = {"a": _unit([1, 0, 0]), "b": _unit([0, 1, 0]), "c": _unit([0, 0, 1]), "d": _unit([1, 1, 0])}


def _index(model=SIG, kind="numpy"):
    index = M.FaissCatalogIndex(embedding_dim=3) if kind == "faiss" else M.NumpyCatalogIndex()
    index.add("cola", np.stack([VEC["a"], VEC["b"]]), ["upload:cola/a", "upload:cola/b"])
    index.add("pepsi", VEC["c"], ["import:c"])
    index.model = model
    return index


class Registry:
    def __init__(self, images, cached):
        self._images, self._cached = images, cached
        self.asked_versions = []

    def images(self, products=None):
        return [i for i in self._images if products is None or i.product_id in products]

    def cached_vectors(self, version):
        self.asked_versions.append(version)
        return dict(self._cached)


PRODUCTS = {"cola": "COLA", "pepsi": "PEPSI"}


def test_photo_deactivated_long_ago_with_a_cached_vector_is_planned_for_removal():
    images = [img("a", "cola", "upload:cola/a", active=False, deactivated_at=LONG_AGO), img("b", "cola", "upload:cola/b"),
              img("c", "pepsi", "import:c", active=False, deactivated_at=RECENTLY)]
    plan = X.plan_prune(_index(), images, {"a": VEC["a"], "b": VEC["b"], "c": VEC["c"]}, PRODUCTS, now=NOW)
    assert plan.remove_rows == [0] and plan.removed == [("cola", "upload:cola/a")]
    assert plan.restore == [] and plan.kept_without_cache == 0 and plan.skipped is None   # недавно выведенное — пока по записи


def test_no_cached_vector_keeps_the_vector_in_the_index():
    images = [img("a", "cola", "upload:cola/a", active=False, deactivated_at=LONG_AGO)]
    plan = X.plan_prune(_index(), images, {}, PRODUCTS, now=NOW)
    assert not plan.changes and plan.kept_without_cache == 1


def test_same_source_still_an_active_reference_is_not_removed():
    """Две записи одного источника у товара (тот же файл записан дважды): пока одна — активный эталон, вектор нужен."""
    images = [img("a", "cola", "upload:cola/a", active=False, deactivated_at=LONG_AGO), img("a2", "cola", "upload:cola/a")]
    assert not X.plan_prune(_index(), images, {"a": VEC["a"], "a2": VEC["a"]}, PRODUCTS, now=NOW).changes


def test_display_photos_and_other_products_and_legacy_vectors_are_left_alone():
    index = _index()
    index.add("cola", VEC["d"], None)                                                     # вектор без источника (до 07.11)
    index.add("ghost", VEC["d"], ["upload:ghost/x"])                                      # товара нет в базе
    images = [img("b", "cola", "upload:cola/b", role="display"),                          # витринное — только по записи
              img("x", "ghost", "upload:ghost/x", active=False, deactivated_at=LONG_AGO)]
    plan = X.plan_prune(index, images, {"b": VEC["b"], "x": VEC["d"]}, PRODUCTS, now=NOW)
    assert not plan.changes


def test_index_without_model_passport_is_not_touched():
    images = [img("a", "cola", "upload:cola/a", active=False, deactivated_at=LONG_AGO)]
    reg = Registry(images, {"a": VEC["a"]})
    index = _index(model=None)
    same, plan = X.prune_index(index, None, reg, PRODUCTS, now=NOW)
    assert same is index and not plan.changes and "паспорт модели" in plan.skipped and reg.asked_versions == []


def test_active_reference_missing_from_the_index_is_restored_from_the_cache():
    index = M.NumpyCatalogIndex()
    index.add("cola", VEC["b"], ["upload:cola/b"])
    index.model = SIG
    images = [img("a", "cola", "upload:cola/a"), img("b", "cola", "upload:cola/b"),
              img("z", "cola", "upload:cola/z")]                                         # нет в кэше — вернуть нечем
    plan = X.plan_prune(index, images, {"a": VEC["a"], "b": VEC["b"]}, PRODUCTS, now=NOW)
    assert [(p, s) for p, s, _v in plan.restore] == [("cola", "upload:cola/a")]
    assert np.array_equal(plan.restore[0][2], VEC["a"])


def test_nothing_is_restored_for_a_product_with_legacy_vectors_or_from_a_wrong_sized_cache():
    index = M.NumpyCatalogIndex()
    index.add("cola", VEC["b"], None)
    index.add("pepsi", VEC["c"], ["import:c"])
    index.model = SIG
    images = [img("a", "cola", "upload:cola/a"), img("p2", "pepsi", "import:p2")]
    plan = X.plan_prune(index, images, {"a": VEC["a"], "p2": np.ones(5, dtype=np.float32)}, PRODUCTS, now=NOW)
    assert plan.restore == []


@pytest.mark.parametrize("kind", ["numpy", "faiss"])
def test_prune_saves_the_index_byte_for_byte_without_the_removed_vector_and_restores_it(tmp_path, kind):
    if kind == "faiss":
        pytest.importorskip("faiss")
    path = str(tmp_path / "catalog.faiss")
    index = _index(kind=kind)
    index.save(path)
    loaded = M.open_catalog_index(path, 3)
    deactivated = img("a", "cola", "upload:cola/a", active=False, deactivated_at=LONG_AGO)
    reg = Registry([deactivated, img("b", "cola", "upload:cola/b"), img("c", "pepsi", "import:c")],
                   {"a": VEC["a"], "b": VEC["b"], "c": VEC["c"]})
    fresh, plan = X.prune_index(loaded, path, reg, PRODUCTS, now=NOW)
    assert plan.removed == [("cola", "upload:cola/a")]
    disk = M.open_catalog_index(path, 3)
    assert disk._ids == ["cola", "pepsi"] and disk._sources == ["upload:cola/b", "import:c"] and disk.model == SIG
    from app.services.index_reconcile import _vectors_of
    assert np.array_equal(_vectors_of(disk), np.stack([VEC["b"], VEC["c"]]))
    assert reg.asked_versions and reg.asked_versions[0].startswith("m1-")

    # вернули в эталоны -> следующий запуск возвращает вектор из кэша; повторный запуск ничего не меняет
    reg._images[0] = img("a", "cola", "upload:cola/a")
    fresh2, plan2 = X.prune_index(fresh, path, reg, PRODUCTS, now=NOW)
    assert plan2.removed == [] and [(p, s) for p, s, _ in plan2.restore] == [("cola", "upload:cola/a")]
    disk = M.open_catalog_index(path, 3)
    assert disk._sources == ["upload:cola/b", "import:c", "upload:cola/a"]
    assert np.array_equal(_vectors_of(disk)[2], VEC["a"])
    same, plan3 = X.prune_index(fresh2, path, reg, PRODUCTS, now=NOW)
    assert same is fresh2 and not plan3.changes


def test_index_written_by_another_process_is_not_overwritten(tmp_path):
    path = str(tmp_path / "catalog.npz")
    _index().save(path)
    ours = M.open_catalog_index(path, 3)
    other = M.open_catalog_index(path, 3)
    other.add("fanta", VEC["d"], ["upload:fanta/1"])
    other.save(path)                                                      # импорт каталога записал файл после нас
    before = Path(path).read_bytes()
    reg = Registry([img("a", "cola", "upload:cola/a", active=False, deactivated_at=LONG_AGO)], {"a": VEC["a"]})
    with pytest.raises(IndexChangedError):
        X.prune_index(ours, path, reg, PRODUCTS, now=NOW)
    assert Path(path).read_bytes() == before                              # чужая запись цела
    assert len(ours._ids) == 3                                            # прежний индекс в памяти не тронут


# ------------------------------------------------------------------ восстановление и отчёт не возвращают убранное


def _photo(uploads: Path, sku: str, name: str) -> str:
    folder = uploads / folder_name(sku)
    folder.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.new("RGB", (8, 8), (200, 10, 10)).save(folder / name, "JPEG")
    return upload_ref(folder.name, name)


class _Embedder:
    embedding_dim = 3

    def __init__(self):
        self.calls = 0

    def embed(self, images):
        self.calls += len(images)
        return np.stack([VEC["a"]] * len(images))


def test_recovery_skips_excluded_photos_and_the_report_does_not_call_them_missing(tmp_path):
    uploads = tmp_path / "catalog-uploads"
    gone = _photo(uploads, "COLA", "001-aaaaaaaaaaaa.jpg")                # выведено, вектор убран
    new = _photo(uploads, "COLA", "002-bbbbbbbbbbbb.jpg")                 # настоящий пропуск
    index = M.NumpyCatalogIndex()
    index.add("pepsi", VEC["c"], ["import:c"])
    skip = frozenset({("cola", gone)})
    rep = C.analyze_coverage(index, {"cola": "COLA"}, uploads, folder_of=folder_name, list_photos=list_photo_files,
                             upload_ref=upload_ref, skip=skip)
    assert [r for _p, _s, r in rep.photos_not_indexed] == [new]
    emb = _Embedder()
    from app.pipeline.imageio import load_rgb
    res = C.recover_pending(index, {"cola": "COLA"}, uploads, embedder=emb, index_path=None, folder_of=folder_name,
                            list_photos=list_photo_files, upload_ref=upload_ref, load_image=load_rgb, skip=skip)
    assert res.added == [("cola", new)] and emb.calls == 1 and gone not in index._sources


def test_worker_prunes_before_recovery_and_passes_the_exclusions():
    src = (Path(__file__).resolve().parents[1] / "app" / "worker.py").read_text(encoding="utf-8")
    body = src[src.index("def recover_index_task"):src.index("_photo_watch = None")]
    assert body.index("_backfill_photos(db, products)") < body.index("_prune_index(db, products, index_path)") \
        < body.index("recover_pending(")
    assert "skip=excluded" in body and '"pruned": pruned' in body
