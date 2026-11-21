"""Изменённые после индексирования фото — обнаружение в плановой проверке recover_index (app/services/photo_watch.py,
app/worker.py: _watch_photo_content). Только сообщается; вектор не заменяется."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import pytest

from app.services.photo_watch import PhotoContentWatch, PhotoRecord


def _photo(root: Path, rel: str, data: bytes, image_id: str = "", product_id: str = "p1") -> PhotoRecord:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return PhotoRecord(image_id or rel, product_id, rel, hashlib.sha256(data).hexdigest())


def _rewrite(root: Path, rec: PhotoRecord, data: bytes) -> None:
    path = root / rec.storage_path
    old = os.stat(path)
    path.write_bytes(data)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 10_000_000))   # время изменения заметно другое


def test_unchanged_photos_are_not_reported_and_not_reread(tmp_path, monkeypatch):
    recs = [_photo(tmp_path, "catalog-uploads/A/001-x.jpg", b"one"), _photo(tmp_path, "catalog-imported/B/2.jpg", b"two")]
    watch = PhotoContentWatch()
    res = watch.check(recs, tmp_path)
    assert res.changed == [] and res.hashed == 2 and res.pending == 0
    import app.services.photo_watch as W
    monkeypatch.setattr(W, "_sha256", lambda p: pytest.fail("перечитан файл, у которого не менялись размер и время"))
    again = watch.check(recs, tmp_path)
    assert again.changed == [] and again.hashed == 0


def test_changed_photo_is_reported_once_as_new_and_then_kept_in_the_total(tmp_path):
    a = _photo(tmp_path, "catalog-imported/A/1.jpg", b"original")
    b = _photo(tmp_path, "catalog-uploads/B/001-y.jpg", b"other")
    watch = PhotoContentWatch()
    watch.check([a, b], tmp_path)
    _rewrite(tmp_path, a, b"edited!!")                         # тот же размер, другое содержимое
    res = watch.check([a, b], tmp_path)
    assert res.changed == [a] and res.newly_changed == [a]
    again = watch.check([a, b], tmp_path)
    assert again.changed == [a] and again.newly_changed == [] and again.hashed == 0
    assert (tmp_path / a.storage_path).read_bytes() == b"edited!!"   # файл не трогаем


def test_photo_changed_before_the_first_check_is_found(tmp_path):
    rec = _photo(tmp_path, "catalog-imported/A/1.jpg", b"original")
    (tmp_path / rec.storage_path).write_bytes(b"broken")
    res = PhotoContentWatch().check([rec], tmp_path)
    assert res.changed == [rec] and res.newly_changed == [rec]


def test_restored_photo_stops_being_reported(tmp_path):
    rec = _photo(tmp_path, "catalog-imported/A/1.jpg", b"original")
    watch = PhotoContentWatch()
    watch.check([rec], tmp_path)
    _rewrite(tmp_path, rec, b"changed")
    assert watch.check([rec], tmp_path).changed == [rec]
    _rewrite(tmp_path, rec, b"original")                       # вернули фото из копии
    assert watch.check([rec], tmp_path).changed == []


def test_budget_spreads_the_first_pass_over_several_runs(tmp_path):
    recs = [_photo(tmp_path, f"catalog-imported/A/{i}.jpg", bytes([i]) * 10) for i in range(7)]
    (tmp_path / recs[6].storage_path).write_bytes(b"bad")
    watch = PhotoContentWatch(budget=3)
    first = watch.check(recs, tmp_path)
    assert (first.hashed, first.pending, first.changed) == (3, 4, [])
    second = watch.check(recs, tmp_path)
    assert (second.hashed, second.pending) == (3, 1)
    third = watch.check(recs, tmp_path)
    assert (third.hashed, third.pending) == (1, 0) and third.changed == [recs[6]]


def test_missing_file_and_path_outside_data_folder_are_skipped(tmp_path):
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"x")
    data = tmp_path / "data"
    data.mkdir()
    recs = [PhotoRecord("1", "p", "catalog-imported/none.jpg", "0" * 64),
            PhotoRecord("2", "p", "../secret.jpg", "0" * 64)]
    res = PhotoContentWatch().check(recs, data)
    assert res.skipped == 2 and res.changed == [] and res.hashed == 0


def test_forgets_records_that_left_recognition(tmp_path):
    rec = _photo(tmp_path, "catalog-imported/A/1.jpg", b"original")
    watch = PhotoContentWatch()
    watch.check([rec], tmp_path)
    watch.check([], tmp_path)
    assert watch._seen == {}


# --------------------------------------------------------------------------- записи из базы и задача воркера

def _db(tmp_path):
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("alembic")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import migrate

    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    migrate.upgrade_to_head(engine)
    return engine, sessionmaker(bind=engine)


def test_reference_records_take_only_active_references_with_a_file(tmp_path):
    from app.db.models import Product, ProductImage
    from app.services.photo_watch import reference_records

    engine, Session = _db(tmp_path)
    db = Session()
    try:
        db.add(Product(id="p1", sku="A", name="Товар", brand="-", category="-"))
        db.flush()
        db.add_all([
            ProductImage(id="i1", product_id="p1", sha256="a" * 64, role="reference", is_active=True,
                         storage_path="catalog-imported/A/1.jpg"),
            ProductImage(id="i2", product_id="p1", sha256="b" * 64, role="reference", is_active=False,
                         storage_path="catalog-imported/A/2.jpg"),
            ProductImage(id="i3", product_id="p1", sha256="c" * 64, role="display", is_active=True,
                         storage_path="catalog-imported/A/3.jpg"),
            ProductImage(id="i4", product_id="p1", sha256="d" * 64, role="reference", is_active=True, storage_path=None),
        ])
        db.commit()
        assert reference_records(db) == [PhotoRecord("i1", "p1", "catalog-imported/A/1.jpg", "a" * 64)]
    finally:
        db.close()
        engine.dispose()


def test_worker_reports_changed_photos_and_survives_a_broken_check(tmp_path, monkeypatch, caplog):
    pytest.importorskip("celery")
    from app import worker
    import app.services.photo_watch as W

    a = _photo(tmp_path, "catalog-imported/A/1.jpg", b"original")
    (tmp_path / a.storage_path).write_bytes(b"changed")
    monkeypatch.setattr(worker, "_media_root", lambda: tmp_path)
    monkeypatch.setattr(worker, "_photo_watch", None)
    monkeypatch.setattr(W, "reference_records", lambda db: [a])

    class Db:
        rolled_back = False

        def rollback(self):
            self.rolled_back = True

    with caplog.at_level(logging.WARNING, logger=worker.logger.name):
        assert worker._watch_photo_content(Db()) == 1
        assert worker._watch_photo_content(Db()) == 1          # повторно — в итоге, но не поимённо
    named = [r for r in caplog.records if a.storage_path in r.getMessage()]
    assert len(named) == 1 and (tmp_path / a.storage_path).read_bytes() == b"changed"

    def broken(db):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(W, "reference_records", broken)
    db = Db()
    assert worker._watch_photo_content(db) is None and db.rolled_back


def test_recover_task_runs_the_photo_check_and_returns_its_count():
    import ast

    src = (Path(__file__).resolve().parents[1] / "app" / "worker.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "recover_index_task")
    body = ast.unparse(fn)
    assert "changed_photos = _watch_photo_content(db)" in body and "'changed_photos': changed_photos" in body
    assert body.index("recover_pending(") < body.index("_watch_photo_content(db)") < body.index("db.close()")
