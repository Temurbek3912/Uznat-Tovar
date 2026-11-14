"""Фото товаров в базе (product_images) и кэш эмбеддингов (image_embeddings) — шаг Р-6 (документ 06, §4.1; документ 05,
§13.3; документ 07, §8): миграция 0005 на пустой и на существующей базе, ключ (фото, версия модели), перенос существующих фото
без изменения файлов, запись фото при добавлении (воркер, импорт, восстановление), пересборка индекса из кэша без модели."""
from __future__ import annotations

import hashlib
import io
import os
import uuid
import sys
import types
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("alembic")

from PIL import Image  # noqa: E402
from alembic import command  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import migrate  # noqa: E402
from app.db.models import ImageEmbedding, Product, ProductImage  # noqa: E402
from app.pipeline import matcher as M  # noqa: E402
from app.pipeline.embedder import model_signature, model_version  # noqa: E402
from app.services import catalog_add as CA  # noqa: E402
from app.services import index_coverage as C  # noqa: E402
from app.services import product_images as P  # noqa: E402
from app.services.catalog_upload import folder_name, list_photo_files, upload_ref  # noqa: E402

SIG = {"name": "color-test", "weights": "sha256:aaa", "preprocess": "mean/v1", "dim": 3, "normalization": "l2"}


class Embedder:
    """Вектор = средний цвет фото; паспорт модели — SIG; считает вызовы модели."""

    embedding_dim = 3

    def __init__(self, signature=SIG):
        self.model_signature = dict(signature)
        self.calls = 0

    def embed(self, images):
        self.calls += len(images)
        out = []
        for img in images:
            v = np.asarray(img, dtype=np.float32).reshape(-1, 3).mean(axis=0) + 1.0
            out.append(v / np.linalg.norm(v))
        return np.stack(out).astype(np.float32)


def _jpeg(rgb):
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), rgb).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _save_photo(uploads: Path, sku: str, n: int, rgb) -> str:
    data = _jpeg(rgb)
    name = f"{n:03d}-{hashlib.sha256(data).hexdigest()[:12]}.jpg"
    folder = uploads / folder_name(sku)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return upload_ref(folder.name, name)


def _load(path):
    return np.array(Image.open(path).convert("RGB"))


@pytest.fixture(params=["sqlite", "postgresql"])
def database(request, tmp_path):
    """SQLite всегда; PostgreSQL — если задан FULL_BACKUP_TEST_DB_URL (тест создаёт и удаляет свою базу)."""
    if request.param == "sqlite":
        yield create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
        return
    url = os.environ.get("FULL_BACKUP_TEST_DB_URL")
    if not url:
        pytest.skip("нет FULL_BACKUP_TEST_DB_URL — временного PostgreSQL")
    from app.db.url import normalize_database_url
    admin = create_engine(normalize_database_url(url), isolation_level="AUTOCOMMIT")
    name = f"img_{uuid.uuid4().hex[:10]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    eng = create_engine(admin.url.set(database=name))
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture
def env(tmp_path, monkeypatch, database):
    monkeypatch.setitem(sys.modules, "faiss", None)                 # numpy-индекс: тесты без FAISS
    engine = database
    migrate.upgrade_to_head(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    e = types.SimpleNamespace(tmp=tmp_path, media=tmp_path / "media", uploads=tmp_path / "media" / "catalog-uploads",
                              path=str(tmp_path / "idx" / "catalog.faiss"), engine=engine, Session=Session, db=db,
                              embedder=Embedder())
    e.registry = P.PhotoRegistry(db)
    e.settings = types.SimpleNamespace(matcher=types.SimpleNamespace(index_path=e.path),
                                       embedder=types.SimpleNamespace(embedding_dim=3, weights_path="x"),
                                       media_root=str(e.media))

    def add_product(pid, sku):
        db.add(Product(id=pid, sku=sku, name=sku, brand="-", category="-", created_at=datetime(2026, 11, 14)))
        db.commit()
    e.add_product = add_product
    e.products = lambda: {pid: sku for pid, sku in db.query(Product.id, Product.sku).all()}
    yield e
    db.close()
    engine.dispose()


def _remember(env):
    def remember(pid, refs, vectors):
        env.registry.remember(pid, [P.upload_photo_info(env.uploads, r) for r in refs], vectors, model_signature(env.embedder))
    return remember


def _add_with_photos(env, pid, sku, colors):
    """Как воркер: товар в базе, фото -> записи + кэш -> индекс."""
    env.add_product(pid, sku)
    refs = [_save_photo(env.uploads, sku, n, c) for n, c in enumerate(colors, start=1)]
    index = M.open_catalog_index(env.path, 3, SIG)
    CA.index_photos(pid, refs, [_load(env.uploads / r[len("upload:"):]) for r in refs], embedder=env.embedder, index=index,
                    index_path=env.path, not_indexed=lambda r: list(r), note_indexed=lambda r: None, remember=_remember(env))
    return refs


# --------------------------------------------------------------------------- миграция


def test_migration_0005_applies_to_an_existing_database_and_keeps_its_data(database):
    engine = database
    with engine.begin() as conn:
        command.upgrade(migrate.alembic_config(conn), "0004")
    with engine.begin() as c:
        c.execute(text("INSERT INTO products (id, sku, name, brand, category, created_at) "
                       "VALUES ('p1', 'KOLA', 'Кола', '-', '-', CURRENT_TIMESTAMP)"))
    result = migrate.upgrade_to_head(engine)
    assert result.before == "0004" and result.after == "0005" == migrate.head_revision()
    tables = set(inspect(engine).get_table_names())
    assert {"product_images", "image_embeddings", "embedding_models"} <= tables
    with engine.connect() as c:
        assert c.execute(text("SELECT sku FROM products")).scalar_one() == "KOLA"
        assert c.execute(text("SELECT COUNT(*) FROM product_images")).scalar_one() == 0
    uniques = inspect(engine).get_unique_constraints("product_images")
    assert any(u["column_names"] == ["product_id", "sha256"] for u in uniques)
    assert inspect(engine).get_pk_constraint("image_embeddings")["constrained_columns"] == ["image_id", "model_version"]
    engine.dispose()


def test_same_photo_twice_for_one_product_is_refused_by_the_database(env):
    env.add_product("p1", "KOLA")
    env.db.add(ProductImage(product_id="p1", sha256="a" * 64, role="reference", is_active=True, created_at=datetime(2026, 11, 14)))
    env.db.commit()
    env.db.add(ProductImage(product_id="p1", sha256="a" * 64, role="reference", is_active=True, created_at=datetime(2026, 11, 14)))
    with pytest.raises(IntegrityError):
        env.db.commit()
    env.db.rollback()


# --------------------------------------------------------------------------- версия модели и кэш


def test_model_version_is_stable_and_changes_with_any_part_of_the_signature():
    assert model_version(SIG) == model_version(dict(reversed(list(SIG.items())))) and model_version(SIG).startswith("m1-")
    for key, value in (("weights", "sha256:bbb"), ("preprocess", "x"), ("dim", 4), ("normalization", "none"), ("name", "y")):
        assert model_version({**SIG, key: value}) != model_version(SIG)
    assert model_version(None) is None and model_version({"name": "x"}) is None


def test_remember_is_idempotent_by_photo_and_model_version(env):
    env.add_product("p1", "KOLA")
    info = P.PhotoInfo(sha256="b" * 64, source_ref="import:" + "b" * 64)
    vec = np.array([[0.6, 0.8, 0.0]], dtype=np.float32)
    first = env.registry.remember("p1", [info], vec, SIG)
    again = env.registry.remember("p1", [info], vec * 0 + 1, SIG)        # повтор: та же запись, вектор не перезаписывается
    other = env.registry.remember("p1", [info], vec[:, ::-1].copy(), {**SIG, "weights": "sha256:new"})
    assert first == again == other
    assert env.db.query(ProductImage).count() == 1 and env.db.query(ImageEmbedding).count() == 2
    cached = env.registry.cached_vectors(model_version(SIG))
    assert np.array_equal(cached[first[0]], vec[0])
    assert env.registry.signature_of(model_version(SIG)) == SIG


def test_vector_bytes_roundtrip_and_corruption_is_detected():
    v = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    assert np.array_equal(P.vector_from_bytes(P.vector_to_bytes(v), 3), v)
    with pytest.raises(ValueError):
        P.vector_from_bytes(P.vector_to_bytes(v)[:8], 3)


# --------------------------------------------------------------------------- перенос существующих фото


def _files_state(root: Path):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def test_backfill_registers_existing_upload_files_without_touching_them(env):
    env.add_product("p1", "KOLA")
    refs = [_save_photo(env.uploads, "KOLA", 1, (200, 10, 10)), _save_photo(env.uploads, "KOLA", 2, (10, 200, 10))]
    before = _files_state(env.uploads)
    kw = dict(folder_of=folder_name, list_photos=list_photo_files, upload_ref=upload_ref)
    preview = P.backfill(env.registry, env.products(), env.uploads, apply=False, **kw)
    assert preview.uploads_registered == 2 and env.db.query(ProductImage).count() == 0        # просмотр ничего не пишет
    res = P.backfill(env.registry, env.products(), env.uploads, **kw)
    assert res.uploads_registered == 2
    rows = env.db.query(ProductImage).order_by(ProductImage.source_ref).all()
    assert [r.source_ref for r in rows] == sorted(refs)
    for r in rows:
        path = env.media / r.storage_path
        assert r.sha256 == hashlib.sha256(path.read_bytes()).hexdigest() and r.role == "reference" and r.is_active
        assert (r.width, r.height) == (32, 32)
    assert _files_state(env.uploads) == before                                                # файлы не тронуты
    assert P.backfill(env.registry, env.products(), env.uploads, **kw).uploads_registered == 0   # повтор безопасен


def test_backfill_takes_import_photos_and_vectors_from_an_index_of_known_model(env):
    env.add_product("p1", "KOLA")
    sha = "c" * 64
    index = M.open_catalog_index(env.path, 3, SIG)
    index.add("p1", np.array([[1.0, 0, 0], [0, 1.0, 0]], dtype=np.float32), [f"import:{sha}", "file:p1/1.jpg"])
    index.add("ghost", np.array([[0, 0, 1.0]], dtype=np.float32), [f"import:{'d' * 64}"])      # товара нет в базе
    index.save(env.path)
    index = M.open_catalog_index(env.path, 3)
    res = P.backfill(env.registry, env.products(), env.uploads, index=index, folder_of=folder_name,
                     list_photos=list_photo_files, upload_ref=upload_ref)
    assert (res.import_registered, res.vectors_cached, res.file_sources_skipped) == (1, 1, 1) and not res.index_model_unknown
    row = env.db.query(ProductImage).one()
    assert row.sha256 == sha and row.storage_path is None and row.product_id == "p1"
    assert np.array_equal(env.registry.cached_vectors(model_version(SIG))[row.id], np.array([1.0, 0, 0], dtype=np.float32))


def test_backfill_does_not_cache_vectors_of_an_unverified_index(env):
    env.add_product("p1", "KOLA")
    index = M.NumpyCatalogIndex()                                    # индекс до 2026-11-14: модель неизвестна
    index.add("p1", np.array([[1.0, 0, 0]], dtype=np.float32), [f"import:{'e' * 64}"])
    res = P.backfill(env.registry, env.products(), env.uploads, index=index, folder_of=folder_name,
                     list_photos=list_photo_files, upload_ref=upload_ref)
    assert res.index_model_unknown and res.import_registered == 1 and res.vectors_cached == 0
    assert env.db.query(ImageEmbedding).count() == 0


# --------------------------------------------------------------------------- запись при добавлении


def test_adding_photos_writes_records_and_cache_before_the_index(env):
    order = []
    env.add_product("p1", "KOLA")
    ref = _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))

    class Index(M.NumpyCatalogIndex):
        def add(self, *a, **k):
            order.append("index")
            super().add(*a, **k)

    def remember(pid, refs, vectors):
        order.append("db")
        _remember(env)(pid, refs, vectors)

    CA.index_photos("p1", [ref], [_load(env.uploads / ref[7:])], embedder=env.embedder, index=Index(), index_path=None,
                    not_indexed=lambda r: list(r), note_indexed=lambda r: None, remember=remember)
    assert order == ["db", "index"]
    row = env.db.query(ProductImage).one()
    assert row.source_ref == ref and env.db.query(ImageEmbedding).one().image_id == row.id


def test_add_product_passes_new_photos_to_remember_but_not_skipped_ones(env):
    seen = []
    store = types.SimpleNamespace(find_id_by_sku=lambda sku: None, create=lambda p: "p9")
    product_in = types.SimpleNamespace(sku="X", reference_image_urls=["upload:X/a.jpg", "upload:X/b.jpg"])
    imgs = [np.full((4, 4, 3), 10, np.uint8), np.full((4, 4, 3), 200, np.uint8)]
    out = CA.add_product(product_in, imgs, store=store, embedder=env.embedder, index=M.NumpyCatalogIndex(), index_path=None,
                         not_indexed=lambda r: [x for x in r if x.endswith("b.jpg")],
                         remember=lambda pid, refs, vecs: seen.append((pid, refs, np.atleast_2d(vecs).shape)))
    assert out.vectors_added == 1 and seen == [("p9", ["upload:X/b.jpg"], (1, 3))]


def test_recover_pending_records_photos_it_indexes(env):
    env.add_product("p1", "KOLA")
    ref = _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    index = M.open_catalog_index(env.path, 3, SIG)
    res = C.recover_pending(index, env.products(), env.uploads, embedder=env.embedder, index_path=env.path,
                            folder_of=folder_name, list_photos=list_photo_files, upload_ref=upload_ref, load_image=_load,
                            remember=_remember(env))
    assert res.added == [("p1", ref)]
    assert env.db.query(ProductImage).one().source_ref == ref and env.db.query(ImageEmbedding).count() == 1


def test_import_writes_records_and_cache(env, tmp_path):
    from app.services import catalog_import as I
    from scripts.import_catalog import SqlCatalogRepo
    folder = tmp_path / "catalog" / "KOLA"
    folder.mkdir(parents=True)
    for n, color in enumerate([(200, 10, 10), (10, 200, 10)], start=1):
        Image.new("RGB", (300, 300), color).save(folder / f"{n}.png")
    folders, _files, _errors = I.scan_catalog_folder(tmp_path / "catalog")
    checks = {p: I.check_photo(p, load=_load) for f in folders for p in f.photos}
    repo = SqlCatalogRepo(env.db)
    plan = I.build_plan(folders, {}, I.Manifest(), checks, repo.existing_products())
    index = M.open_catalog_index(env.path, 3, SIG)
    res = I.run_import(plan, repo, env.embedder, index, index_path=env.path, manifest=I.Manifest(),
                       manifest_path=tmp_path / "m.json", load=_load, out=lambda s: None,
                       remember=lambda pid, sku, photos, emb: repo.remember_photos(pid, sku, photos, emb, SIG))
    assert res.created == 1 and res.photos_added == 2 and not res.failed
    rows = env.db.query(ProductImage).all()
    assert sorted(r.source_ref for r in rows) == sorted(index._sources)
    assert all(r.storage_path is None and r.original_name.startswith("KOLA/") for r in rows)
    assert env.db.query(ImageEmbedding).count() == 2


# --------------------------------------------------------------------------- пересборка индекса из кэша


def test_index_rebuilt_from_cache_without_the_model_matches_the_original(env):
    _add_with_photos(env, "p1", "KOLA", [(200, 10, 10), (190, 20, 10)])
    _add_with_photos(env, "p2", "FANTA", [(10, 10, 200)])
    original = M.open_catalog_index(env.path, 3, SIG)
    plan = P.plan_rebuild(env.registry, env.products(), model_version(SIG))
    assert len(plan.cached) == 3 and not plan.missing
    calls = env.embedder.calls
    rebuilt = P.build_index(plan, SIG, 3).index                       # без модели
    assert env.embedder.calls == calls
    assert sorted(zip(rebuilt._ids, rebuilt._sources)) == sorted(zip(original._ids, original._sources))
    query = np.asarray(original._vectors[2])
    assert rebuilt.search(query, 1) == original.search(query, 1) and rebuilt.model == SIG


def test_inactive_and_display_photos_are_left_out_of_the_rebuilt_index(env):
    refs = _add_with_photos(env, "p1", "KOLA", [(200, 10, 10), (10, 200, 10), (10, 10, 200)])
    rows = {r.source_ref: r for r in env.db.query(ProductImage).all()}
    env.registry.set_active(rows[refs[0]].id, False)
    rows[refs[1]].role = P.DISPLAY
    env.db.commit()
    plan = P.plan_rebuild(env.registry, env.products(), model_version(SIG))
    assert plan.inactive == 1 and [i.source for i in plan.items] == [refs[2]]
    assert (env.uploads / refs[0][7:]).is_file()                       # выведенное фото не удалено
    assert env.db.get(ProductImage, rows[refs[0]].id).deactivated_at is not None


def test_photos_without_cache_need_the_model_or_explicit_consent(env):
    env.add_product("p1", "KOLA")
    _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    P.backfill(env.registry, env.products(), env.uploads, folder_of=folder_name, list_photos=list_photo_files,
               upload_ref=upload_ref)
    env.registry.ensure_model(SIG)
    plan = P.plan_rebuild(env.registry, env.products(), model_version(SIG))
    assert len(plan.missing) == 1
    with pytest.raises(RuntimeError):
        P.build_index(plan, SIG, 3)
    assert P.build_index(plan, SIG, 3, allow_missing=True).left_out
    res = P.build_index(plan, SIG, 3, registry=env.registry, embedder=env.embedder, media_root=env.media, load_image=_load)
    assert res.computed == 1 and len(res.index._ids) == 1
    assert len(env.registry.cached_vectors(model_version(SIG))) == 1        # посчитанное — в кэше: в следующий раз без модели
    with pytest.raises(ValueError):
        P.build_index(plan, SIG, 3, embedder=Embedder({**SIG, "weights": "other"}))


def _script_kw(env, **extra):
    lines = []
    kw = dict(out=lines.append, settings=env.settings, resolve_path=lambda p: Path(p), make_session=env.Session, **extra)
    return lines, kw


def test_rebuild_script_restores_a_lost_index_from_cache(env):
    from scripts import rebuild_index
    _add_with_photos(env, "p1", "KOLA", [(200, 10, 10), (190, 20, 10)])
    original = M.open_catalog_index(env.path, 3, SIG)
    Path(env.path + ".npz").unlink()                                   # индекс потерян
    lines, kw = _script_kw(env)
    assert rebuild_index.main([], **kw) == 0 and not Path(env.path + ".npz").exists()   # просмотр ничего не пишет
    assert rebuild_index.main(["--apply", "--yes"], **kw) == 0, "\n".join(lines)
    restored = M.open_catalog_index(env.path, 3, SIG)
    assert sorted(zip(restored._ids, restored._sources)) == sorted(zip(original._ids, original._sources))


def test_rebuild_script_refuses_to_drop_vectors_and_keeps_a_backup(env):
    from scripts import rebuild_index
    _add_with_photos(env, "p1", "KOLA", [(200, 10, 10)])
    index = M.open_catalog_index(env.path, 3, SIG)
    index.add("p1", np.array([[0, 0, 1.0]], dtype=np.float32), [None])     # старый вектор без записи о фото
    index.save(env.path)
    lines, kw = _script_kw(env)
    assert rebuild_index.main(["--apply", "--yes"], **kw) == 1 and "Отказ" in "\n".join(lines)
    assert len(M.open_catalog_index(env.path, 3, SIG)._ids) == 2                # не тронут
    assert rebuild_index.main(["--apply", "--yes", "--allow-missing"], **kw) == 0
    assert M.open_catalog_index(env.path, 3, SIG)._ids == ["p1"]
    backups = list(Path(env.path).parent.glob("backup-*"))
    assert backups and any(p.name.endswith(".npz") for p in backups[0].iterdir())


def test_rebuild_script_computes_missing_vectors_with_the_model(env):
    from scripts import rebuild_index
    env.add_product("p1", "KOLA")
    _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    lines, kw = _script_kw(env, make_embedder=lambda s: env.embedder, load_image=_load)
    assert rebuild_index.main(["--apply", "--yes"], **kw) == 2               # модель неизвестна, кэш пуст
    assert rebuild_index.main(["--apply", "--yes", "--compute-missing"], **kw) == 0, "\n".join(lines)
    assert len(M.open_catalog_index(env.path, 3, SIG)._ids) == 1                # фото из папки сначала записано в базу
    env.db.expire_all()
    assert env.db.query(ProductImage).count() == 1 and env.db.query(ImageEmbedding).count() == 1


def test_register_photos_script_preview_then_apply(env):
    from scripts import register_photos
    env.add_product("p1", "KOLA")
    _save_photo(env.uploads, "KOLA", 1, (200, 10, 10))
    lines, kw = _script_kw(env)
    assert register_photos.main([], **kw) == 0 and env.db.query(ProductImage).count() == 0
    assert register_photos.main(["--apply"], **kw) == 0
    env.db.expire_all()
    assert env.db.query(ProductImage).count() == 1
