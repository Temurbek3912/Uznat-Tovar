"""
База данных — единственный источник истины о товарах (app/services/catalog_add.py, app/services/identify_service.py; 2026-11-07).

Проверяется без SQLAlchemy и Celery: база — словарь с «commit», индекс — настоящий NumpyCatalogIndex на диске (через
open_catalog_index, как у воркера). Главные свойства:
  - товар фиксируется в базе ДО записи векторов; в индексе на диске никогда нет id, которого нет в базе;
  - повтор после любого сбоя доводит дело до конца без дублей;
  - распознавание никогда не называет товар, которого нет в базе, и не теряет настоящий товар из-за чужих векторов.
"""
from __future__ import annotations

import random
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import matcher as M
from app.services import catalog_add as CA
from app.services.identify_service import ProductInfo, identify


class Boom(Exception):
    pass


class FakeStore:
    """products.sku уникален; create() — это commit; fail_create — сбой базы."""

    def __init__(self, log):
        self.rows: dict[str, str] = {}          # sku -> id
        self.log = log
        self.fail_create = False
        self.race_winner: str | None = None     # «другой запрос» успевает создать товар с тем же артикулом

    def find_id_by_sku(self, sku):
        return self.rows.get(sku)

    def create(self, product_in):
        self.log.append("create")
        if self.fail_create:
            raise Boom("база недоступна")
        if self.race_winner is not None:
            self.rows[product_in.sku] = self.race_winner
            raise CA.DuplicateSku(product_in.sku)
        if product_in.sku in self.rows:
            raise CA.DuplicateSku(product_in.sku)
        pid = f"id-{len(self.rows) + 1}-{product_in.sku}"
        self.rows[product_in.sku] = pid
        return pid

    def ids(self):
        return set(self.rows.values())


class FakeEmbedder:
    def __init__(self, log):
        self.log, self.fail = log, False

    def embed(self, images):
        self.log.append("embed")
        if self.fail:
            raise Boom("модель упала")
        return np.stack([img.reshape(-1)[:3].astype(np.float32) / (np.linalg.norm(img.reshape(-1)[:3]) or 1) for img in images])


def _img(r, g, b):
    return np.array([r, g, b], dtype=np.float32).reshape(1, 1, 3)


def _product(sku, n=2):
    refs = [f"upload:{sku}/{i:03d}-abc{i}.jpg" for i in range(1, n + 1)]
    return types.SimpleNamespace(sku=sku, name=sku, brand="B", category="C", expected_price=None, reference_image_urls=refs)


class Journal:
    def __init__(self, log):
        self.keys, self.log = set(), log

    def not_indexed(self, refs):
        return [r for r in refs if r not in self.keys]

    def note(self, refs):
        self.log.append("note")
        self.keys.update(refs)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "faiss", None)              # numpy-индекс: тот же контракт сохранения и проверок
    log = []
    e = types.SimpleNamespace(log=log, store=FakeStore(log), embedder=FakeEmbedder(log), journal=Journal(log),
                              path=str(tmp_path / "catalog.faiss"))
    e.matcher = M.ProductMatcher(M.open_catalog_index(e.path, 3))
    return e


def _run(env, product_in, images):
    """Как задача воркера: при сбое — откат памяти к диску (resync_index) и исключение наверх."""
    size_before = M.index_size(env.matcher.index)
    try:
        return CA.add_product(product_in, images, store=env.store, embedder=env.embedder, index=_spy_index(env),
                              index_path=env.path, not_indexed=env.journal.not_indexed, note_indexed=env.journal.note)
    except BaseException:
        if M.index_size(env.matcher.index) != size_before:
            assert M.resync_index(env.matcher, env.path, 3) is None
        raise


def _spy_index(env):
    index = env.matcher.index
    real_add, real_save = index.add, index.save

    def add(pid, vectors, sources=None):
        env.log.append("index.add")
        if getattr(env, "fail_add", False):
            raise Boom("add")
        real_add(pid, vectors, sources)

    def save(path):
        env.log.append("index.save")
        if getattr(env, "fail_save", False):
            raise Boom("save")
        real_save(path)

    index.add, index.save = add, save
    return index


def _disk_ids(env):
    p = Path(env.path + ".npz")
    return set(M.NumpyCatalogIndex.load(env.path)._ids) if p.exists() else set()


IMGS = [_img(200, 10, 10), _img(10, 200, 10)]


def test_order_is_embed_then_database_commit_then_index_then_journal(env):
    out = _run(env, _product("KOLA"), IMGS)
    assert env.log == ["embed", "create", "index.add", "index.save", "note"]
    assert out.created and out.vectors_added == 2 and _disk_ids(env) == env.store.ids()


def test_model_failure_creates_nothing(env):
    env.embedder.fail = True
    with pytest.raises(Boom):
        _run(env, _product("KOLA"), IMGS)
    assert "create" not in env.log and env.store.rows == {} and _disk_ids(env) == set()


def test_database_failure_writes_no_vectors(env):
    env.store.fail_create = True
    with pytest.raises(Boom):
        _run(env, _product("KOLA"), IMGS)
    assert "index.add" not in env.log and _disk_ids(env) == set() and M.index_size(env.matcher.index) == 0


@pytest.mark.parametrize("stage", ["fail_add", "fail_save"])
def test_index_failure_after_commit_leaves_a_product_without_vectors_and_a_retry_completes_it(env, stage):
    setattr(env, stage, True)
    with pytest.raises(Boom):
        _run(env, _product("KOLA"), IMGS)
    assert list(env.store.rows) == ["KOLA"] and _disk_ids(env) == set()            # товар есть, векторов нет — не «призрак»
    assert M.index_size(env.matcher.index) == 0                                       # память откатилась к диску
    setattr(env, stage, False)
    out = _run(env, _product("KOLA"), IMGS)                                            # повтор задачи
    assert not out.created and out.vectors_added == 2
    assert env.store.ids() == _disk_ids(env)
    assert sorted(M.NumpyCatalogIndex.load(env.path)._ids) == [env.store.rows["KOLA"]] * 2


def test_repeat_after_full_success_adds_nothing(env):
    _run(env, _product("KOLA"), IMGS)
    out = _run(env, _product("KOLA"), IMGS)
    assert not out.created and out.vectors_added == 0 and len(out.skipped_refs) == 2
    assert len(M.NumpyCatalogIndex.load(env.path)._ids) == 2


def test_journal_failure_after_save_only_risks_a_duplicate_vector_never_a_ghost(env):
    def broken_note(refs):
        raise Boom("журнал")
    with pytest.raises(Boom):
        CA.add_product(_product("KOLA"), IMGS, store=env.store, embedder=env.embedder, index=env.matcher.index, index_path=env.path,
                       not_indexed=env.journal.not_indexed, note_indexed=broken_note)
    assert _disk_ids(env) == env.store.ids()


def test_concurrent_creation_with_the_same_sku_uses_the_winner(env):
    env.store.race_winner = "id-winner"
    out = _run(env, _product("KOLA"), IMGS)
    assert out.product_id == "id-winner" and not out.created and _disk_ids(env) == {"id-winner"}


def test_duplicate_error_without_a_winner_is_not_swallowed(env):
    class Weird(FakeStore):
        def create(self, product_in):
            raise CA.DuplicateSku("другая уникальность")
    env.store = Weird(env.log)
    with pytest.raises(CA.DuplicateSku):
        _run(env, _product("KOLA"), IMGS)


def test_random_failures_never_leave_vectors_of_a_product_missing_in_the_database(env):
    """500 операций со случайными сбоями на каждом шаге и повторами: на диске — только id из базы; после повторов все товары с
    векторами и без дублей."""
    rnd = random.Random(7)
    skus = [f"S{i}" for i in range(25)]
    for _ in range(500):
        sku = rnd.choice(skus)
        env.embedder.fail = rnd.random() < 0.1
        env.store.fail_create = rnd.random() < 0.1
        env.fail_add = rnd.random() < 0.1
        env.fail_save = rnd.random() < 0.1
        try:
            _run(env, _product(sku), IMGS)
        except Boom:
            pass
        assert _disk_ids(env) <= env.store.ids()
        assert set(env.matcher.index._ids) <= env.store.ids()                 # и в памяти тоже
    env.embedder.fail = env.store.fail_create = env.fail_add = env.fail_save = False
    for sku in skus:                                                              # «повторы задач»
        _run(env, _product(sku), IMGS)
    ids = M.NumpyCatalogIndex.load(env.path)._ids
    assert set(ids) == env.store.ids() and len(ids) == 2 * len(env.store.ids())   # у каждого товара ровно 2 вектора


# --------------------------------------------------------------------------- распознавание: только товары из базы

class ColorEmbedder:
    def embed(self, images):
        out = []
        for img in images:
            v = img.reshape(-1, 3).mean(axis=0).astype(np.float32)
            out.append(v / (np.linalg.norm(v) or 1))
        return np.stack(out)


def _frame(r, g, b):
    return np.full((8, 8, 3), (r, g, b), dtype=np.uint8)


def test_real_product_behind_many_ghost_vectors_is_still_found():
    """30 векторов удалённых из базы товаров ближе к кадру, чем настоящий товар: раньше проверялись только 8 лучших товаров —
    настоящий не попадал в выдачу, ответ «не знаю»."""
    index = M.NumpyCatalogIndex()
    emb = ColorEmbedder()
    for i in range(30):
        index.add(f"ghost-{i}", emb.embed([_frame(200, 20 + i % 3, 20)]))
    index.add("real", emb.embed([_frame(180, 60, 40)]))
    index.add("other", emb.embed([_frame(20, 20, 200)]))
    matcher = M.ProductMatcher(index, match_threshold=0.9, unknown_threshold=0.5)
    db = {"real": ProductInfo("real", "Кола"), "other": ProductInfo("other", "Вода")}
    result = identify(_frame(200, 22, 20), emb, matcher, lambda ids: {i: db[i] for i in ids if i in db})
    assert result["product"] is not None and result["product"]["id"] == "real"
    assert {c["id"] for c in result["candidates"]} <= set(db)


def test_no_deep_search_when_nothing_is_missing():
    index = M.NumpyCatalogIndex()
    emb = ColorEmbedder()
    for i in range(40):
        index.add(f"p{i}", emb.embed([_frame(10 + i * 5, 50, 50)]))
    asked = []
    matcher = M.ProductMatcher(index)
    identify(_frame(100, 50, 50), emb, matcher, lambda ids: (asked.append(list(ids)), {i: ProductInfo(i, i) for i in ids})[1])
    assert len(asked) == 1


def test_identify_never_names_a_product_missing_in_the_database_property():
    rnd = np.random.default_rng(3)
    emb = ColorEmbedder()
    for trial in range(60):
        index = M.NumpyCatalogIndex()
        ids = [f"p{i}" for i in range(rnd.integers(1, 40))]
        for pid in ids:
            for _ in range(rnd.integers(1, 4)):
                index.add(pid, emb.embed([_frame(*rnd.integers(0, 255, 3))]))
        in_db = {pid for pid in ids if rnd.random() < 0.5}
        matcher = M.ProductMatcher(index, match_threshold=0.95, unknown_threshold=0.8)
        res = identify(_frame(*rnd.integers(0, 255, 3)), emb, matcher, lambda q: {i: ProductInfo(i, i) for i in q if i in in_db})
        named = ([res["product"]["id"]] if res["product"] else []) + [c["id"] for c in res["candidates"]]
        assert set(named) <= in_db, (trial, named, in_db)


def test_each_vector_records_which_photo_it_came_from(env):
    """Этап 4: без источника нельзя ни обнаружить недостающее фото, ни восстановить точечно."""
    product = _product("KOLA")
    _run(env, product, IMGS)
    loaded = M.NumpyCatalogIndex.load(env.path)
    assert loaded._sources == product.reference_image_urls
