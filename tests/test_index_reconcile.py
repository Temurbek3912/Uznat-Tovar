"""
Сверка индекса с базой и восстановление (app/services/index_reconcile.py, scripts/reconcile_index.py; 2026-11-07).

Главные свойства: восстановление никогда не удаляет векторы товаров, которые есть в базе, не трогает фото, делает копию до
изменений, проверяет результат и отказывает без изменений, если данные похожи на «не ту базу» или индекс записан другим процессом.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import matcher as M
from app.services import index_reconcile as R
from app.services.catalog_upload import folder_name, record_indexed
from tests.test_index_integrity import _make_fake_faiss, _unit


@pytest.fixture(params=["faiss", "numpy"])
def backend(request, monkeypatch):
    monkeypatch.setitem(sys.modules, "faiss", _make_fake_faiss() if request.param == "faiss" else None)
    return request.param


def _build(path: Path, items):
    index = M.open_catalog_index(str(path), 3)
    for pid, vec in items:
        index.add(pid, _unit(*vec))
    index.save(str(path))
    return index


ITEMS = [("p1", (1, 0, 0)), ("ghost", (0, 1, 0)), ("p1", (1, 0.1, 0)), ("p2", (0, 0, 1)), ("p3", (1, 1, 0))]
PRODUCTS = {"p1": "KOLA", "p2": "FANTA", "p3": "SPRITE", "p4": "WATER"}          # p4 — в базе, но без векторов


def _files_snapshot(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file() and not p.name.endswith(".lock")}


def test_analyze_finds_ghosts_products_without_vectors_and_stale_journal_marks(backend, tmp_path):
    path = tmp_path / "idx" / "catalog.faiss"
    index = _build(path, ITEMS)
    report = R.analyze(index, PRODUCTS, photo_counts={"WATER": 3}, folder_of=folder_name,
                       journal_keys={"WATER/001-a.jpg", "KOLA/001-b.jpg"})
    assert report.ghosts == {"ghost": 1} and report.without_vectors == [("p4", "WATER")]
    assert report.recoverable == [("p4", "WATER", 3)] and report.stale_journal_keys == ["WATER/001-a.jpg"]
    assert report.blockers == [] and not report.clean


def test_apply_removes_only_ghosts_keeps_valid_vectors_byte_for_byte_and_makes_a_verified_backup(backend, tmp_path):
    path = tmp_path / "idx" / "catalog.faiss"
    index = _build(path, ITEMS)
    before_vectors = R._vectors_of(index)
    photos = tmp_path / "media" / "catalog-uploads" / "WATER"
    photos.mkdir(parents=True)
    (photos / "001-abc.jpg").write_bytes(b"jpeg")
    assert record_indexed(str(path), ["upload:WATER/001-abc.jpg", "upload:KOLA/001-def.jpg"])
    journal = path.parent / "uploads_manifest.json"
    old_files = _files_snapshot(path.parent)
    res = R.apply(str(path), 3, PRODUCTS, journal_path=journal, folder_of=folder_name, out=lambda *_: None)
    assert res.removed_vectors == 1 and res.kept_vectors == 4 and res.verified and res.journal_keys_removed == 1
    loaded = M.open_catalog_index(str(path), 3)
    assert loaded._ids == ["p1", "p1", "p2", "p3"]
    keep_rows = [i for i, pid in enumerate(index._ids) if pid != "ghost"]
    assert np.array_equal(R._vectors_of(loaded), before_vectors[keep_rows])
    assert json.loads(journal.read_text(encoding="utf-8"))["photos"] == ["KOLA/001-def.jpg"]
    backup = {k.split("/", 1)[1]: v for k, v in _files_snapshot(path.parent).items() if k.startswith("backup-")}
    assert backup == {k: v for k, v in old_files.items() if not k.startswith("backup-")}     # копия = прежние файлы
    assert (photos / "001-abc.jpg").read_bytes() == b"jpeg"                                   # фото не тронуты


def test_apply_refuses_when_the_database_is_empty(backend, tmp_path):
    path = tmp_path / "catalog.faiss"
    _build(path, ITEMS)
    before = _files_snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="не та база"):
        R.apply(str(path), 3, {}, out=lambda *_: None)
    assert _files_snapshot(tmp_path) == before


def test_apply_refuses_when_most_vectors_have_no_product_like_an_index_built_from_sku_folders(backend, tmp_path):
    path = tmp_path / "catalog.faiss"
    _build(path, [("KOLA", (1, 0, 0)), ("FANTA", (0, 1, 0)), ("p1", (0, 0, 1))])          # id = артикулы, а не id базы
    before = _files_snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="другой базы"):
        R.apply(str(path), 3, {"p1": "X1", "uuid-kola": "KOLA"}, out=lambda *_: None)
    assert _files_snapshot(tmp_path) == before


def test_apply_refuses_without_changes_when_another_process_saved_meanwhile(backend, tmp_path, monkeypatch):
    path = tmp_path / "catalog.faiss"
    _build(path, ITEMS)
    real_backup = R.backup_files

    def backup_then_worker_saves(index_path, extra=()):
        target = real_backup(index_path, extra)
        worker = M.open_catalog_index(index_path, 3)
        worker.add("p4", _unit(0.5, 0.5, 0.5))
        worker.save(index_path)                                   # воркер добавил фото товару p4
        return target

    monkeypatch.setattr(R, "backup_files", backup_then_worker_saves)
    with pytest.raises(M.IndexChangedError):
        R.apply(str(path), 3, PRODUCTS, out=lambda *_: None)
    assert "p4" in M.open_catalog_index(str(path), 3)._ids         # запись воркера не затёрта


def test_clean_index_changes_nothing(backend, tmp_path):
    path = tmp_path / "catalog.faiss"
    _build(path, [("p1", (1, 0, 0))])
    before = _files_snapshot(tmp_path)
    res = R.apply(str(path), 3, {"p1": "KOLA"}, out=lambda *_: None)
    assert res.removed_vectors == 0 and res.verified
    after = {k: v for k, v in _files_snapshot(tmp_path).items() if not k.startswith("backup-")}
    assert after == before


# --------------------------------------------------------------------------- скрипт

def _settings(tmp_path):
    return types.SimpleNamespace(matcher=types.SimpleNamespace(index_path=str(tmp_path / "idx" / "catalog.faiss")),
                                 embedder=types.SimpleNamespace(embedding_dim=3), media_root=str(tmp_path / "media"))


def _main(tmp_path, argv, products):
    from scripts import reconcile_index
    lines = []
    code = reconcile_index.main(argv, out=lines.append, load_products=lambda: dict(products), settings=_settings(tmp_path),
                                resolve_path=lambda p: Path(p))
    return code, "\n".join(lines)


def test_script_report_only_changes_nothing(backend, tmp_path):
    _build(tmp_path / "idx" / "catalog.faiss", ITEMS)
    before = _files_snapshot(tmp_path)
    code, text = _main(tmp_path, [], PRODUCTS)
    assert code == 0 and "Векторы товаров, которых нет в базе: 1" in text and "только отчёт" in text
    assert _files_snapshot(tmp_path) == before


def test_script_apply_with_yes_repairs_and_reports(backend, tmp_path):
    _build(tmp_path / "idx" / "catalog.faiss", ITEMS)
    code, text = _main(tmp_path, ["--apply", "--yes"], PRODUCTS)
    assert code == 0 and "убрано векторов 1" in text and "пройдена" in text
    assert "ghost" not in M.open_catalog_index(str(tmp_path / "idx" / "catalog.faiss"), 3)._ids


def test_script_blocked_apply_exits_with_an_error_and_changes_nothing(backend, tmp_path):
    _build(tmp_path / "idx" / "catalog.faiss", ITEMS)
    before = _files_snapshot(tmp_path)
    code, text = _main(tmp_path, ["--apply", "--yes"], {})
    assert code == 2 and "ОТКАЗ" in text and _files_snapshot(tmp_path) == before


def test_script_reports_an_unloadable_index_without_touching_it(backend, tmp_path):
    path = tmp_path / "idx" / "catalog.faiss"
    _build(path, ITEMS)
    target = path if backend == "faiss" else Path(str(path) + ".npz")
    target.write_bytes(b"broken")
    before = _files_snapshot(tmp_path)
    code, text = _main(tmp_path, ["--apply", "--yes"], PRODUCTS)
    assert code == 2 and "не загружается" in text and _files_snapshot(tmp_path) == before


# --------------------------------------------------------------------------- полная пересборка не затирает рабочий индекс молча

def test_full_rebuild_into_a_free_path_needs_nothing(backend, tmp_path):
    assert R.prepare_full_rebuild(str(tmp_path / "catalog.faiss"), replace_existing=False) is None


def test_full_rebuild_over_an_existing_index_is_refused_without_the_flag(backend, tmp_path):
    path = tmp_path / "catalog.faiss"
    _build(path, ITEMS)
    before = _files_snapshot(tmp_path)
    with pytest.raises(SystemExit, match="replace-existing"):
        R.prepare_full_rebuild(str(path), replace_existing=False)
    assert _files_snapshot(tmp_path) == before


def test_full_rebuild_with_the_flag_makes_a_verified_backup_first(backend, tmp_path):
    path = tmp_path / "catalog.faiss"
    _build(path, ITEMS)
    assert record_indexed(str(path), ["upload:KOLA/001-a.jpg"])
    before = _files_snapshot(tmp_path)
    backup = R.prepare_full_rebuild(str(path), replace_existing=True, journal_path=tmp_path / "uploads_manifest.json")
    copied = {p.name: p.read_bytes() for p in backup.iterdir()}
    assert copied == before


def test_rebuild_script_checks_the_target_before_loading_the_model():
    src = (Path(__file__).resolve().parents[1] / "ml" / "build_catalog_embeddings.py").read_text(encoding="utf-8")
    assert "--replace-existing" in src and src.index("prepare_full_rebuild(") < src.index("embedder = create_embedder(")
