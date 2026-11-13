"""
Безопасная очистка временных файлов (app/services/temp_cleanup.py, scripts/cleanup_temp.py; 2026-11-07).

Главное: не удалить файл, которым пользуется работающий процесс, и не удалить ничего, кроме временных файлов программы;
журнал каждого действия; проверка результата.
"""
from __future__ import annotations

import errno
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

from app.pipeline import index_store
from app.pipeline import matcher as M
from app.services import temp_cleanup as T
from tests.test_index_integrity import _ProcessDied, _die_on_replace, _faiss_index, _make_fake_faiss, _unit

LATER = time.time() + 7200                     # «через два часа»: все временные файлы старше любых порогов


@pytest.fixture
def fake_faiss(monkeypatch):
    mod = _make_fake_faiss()
    monkeypatch.setitem(sys.modules, "faiss", mod)
    return mod


def _abandoned_save(fake_faiss, path, monkeypatch, nth=1):
    """Запись погибла (до первой замены — nth=1, между заменами — nth=2); уборки в except не было."""
    real_replace = _die_on_replace(monkeypatch, nth, _ProcessDied())
    monkeypatch.setattr(M, "discard", lambda p: None)
    index = M.open_catalog_index(str(path), 3)
    index.add("fanta", _unit(0, 1, 0))
    with pytest.raises(_ProcessDied):
        index.save(str(path))
    monkeypatch.setattr(os, "replace", real_replace)
    return sorted(p for p in path.parent.iterdir() if ".tmp-" in p.name)


def _setup(fake_faiss, tmp_path, monkeypatch, nth=1):
    path = tmp_path / "idx" / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    temps = _abandoned_save(fake_faiss, path, monkeypatch, nth)
    monkeypatch.setattr(M, "discard", index_store.discard)
    return path, temps


def _run(path, uploads=None, **kw):
    lines = []
    res = T.cleanup(str(path), 3, uploads, log=lines.append, **kw)
    return res, "\n".join(lines)


def test_abandoned_index_temp_files_are_removed_under_the_lock_and_the_index_is_verified(fake_faiss, tmp_path, monkeypatch):
    path, temps = _setup(fake_faiss, tmp_path, monkeypatch)
    assert len(temps) == 2
    res, log = _run(path, apply=True, now=LATER)
    assert sorted(res.deleted) == temps and res.verified and not res.errors
    assert not [p for p in path.parent.iterdir() if ".tmp-" in p.name]
    assert M.FaissCatalogIndex.load(str(path))._ids == ["cola", "chips"]
    assert all(f"удалён: {p}" in log for p in temps)


def test_dry_run_deletes_nothing_but_reports(fake_faiss, tmp_path, monkeypatch):
    path, temps = _setup(fake_faiss, tmp_path, monkeypatch)
    res, log = _run(path, apply=False, now=LATER)
    assert res.deleted == [] and all(p.exists() for p in temps) and log.count("будет удалён") == 2


def test_young_temp_files_are_kept(fake_faiss, tmp_path, monkeypatch):
    path, temps = _setup(fake_faiss, tmp_path, monkeypatch)
    res, _ = _run(path, apply=True)                                   # «сейчас»: файлам секунды
    assert res.deleted == [] and all(p.exists() for p in temps)
    assert all("моложе" in why for _p, why in res.kept)


def test_files_are_kept_while_another_process_holds_the_index(fake_faiss, tmp_path, monkeypatch):
    path, temps = _setup(fake_faiss, tmp_path, monkeypatch)
    monkeypatch.setattr(index_store, "LOCK_TIMEOUT_SECONDS", 0.2)
    with index_store.IndexLock(path) as held:
        assert held.locked
        res, _ = _run(path, apply=True, now=LATER)
    assert res.deleted == [] and all(p.exists() for p in temps) and all("занят" in why for _p, why in res.kept)


def test_files_are_kept_when_the_filesystem_has_no_locks(fake_faiss, tmp_path, monkeypatch):
    path, temps = _setup(fake_faiss, tmp_path, monkeypatch)

    def no_locks(fh):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(index_store, "_try_lock", no_locks)
    res, _ = _run(path, apply=True, now=LATER)
    assert res.deleted == [] and all(p.exists() for p in temps)
    assert all("блокировку" in why for _p, why in res.kept)


def test_files_are_kept_when_the_index_does_not_load(fake_faiss, tmp_path, monkeypatch):
    path, temps = _setup(fake_faiss, tmp_path, monkeypatch)
    (path.parent / "catalog.faiss.ids.json").unlink()                # индекс непригоден: временные файлы — возможная опора
    res, _ = _run(path, apply=True, now=LATER)
    assert res.deleted == [] and all(p.exists() for p in temps)


def test_interrupted_save_between_replacements_is_completed_first_not_deleted(fake_faiss, tmp_path, monkeypatch):
    path, temps = _setup(fake_faiss, tmp_path, monkeypatch, nth=2)
    assert len(temps) == 1 and temps[0].name.startswith("catalog.faiss.ids.json.tmp-")
    res, _ = _run(path, apply=True, now=LATER)
    assert res.verified and M.FaissCatalogIndex.load(str(path))._ids == ["cola", "chips", "fanta"]      # запись завершена
    assert not [p for p in path.parent.iterdir() if ".tmp-" in p.name]


def test_legacy_id_list_without_checksum_keeps_temp_id_lists(fake_faiss, tmp_path, monkeypatch):
    path, temps = _setup(fake_faiss, tmp_path, monkeypatch)
    sidecar = path.parent / "catalog.faiss.ids.json"
    sidecar.write_text(json.dumps(json.loads(sidecar.read_text(encoding="utf-8"))["ids"]), encoding="utf-8")   # формат 1
    res, _ = _run(path, apply=True, now=LATER)
    assert res.deleted == [] and all("не согласованы" in why for _p, why in res.kept)


def test_photo_and_journal_temp_files_old_ones_removed_real_files_never(fake_faiss, tmp_path):
    path = tmp_path / "idx" / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    uploads = tmp_path / "media" / "catalog-uploads"
    folder = uploads / "KOLA"
    folder.mkdir(parents=True)
    keep = {
        folder / "001-0123456789ab.jpg": b"photo",                          # фото
        folder / "thumb-001.jpg": b"thumb",
        folder / "notes.part": b"not ours",                                 # похоже, но не наш шаблон
        path.parent / "uploads_manifest.json": b"{}",
        path.parent / "backup-20261107-1200-abcdef" / "catalog.faiss": b"backup",
    }
    remove = {
        folder / "002-0123456789ab.jpg.a1b2c3d4e5f6.part": b"half",          # новый шаблон
        folder / "003-0123456789ab.jpg.part": b"half",                       # прежний шаблон
        path.parent / "uploads_manifest.json.a1b2c3d4e5f6.part": b"{",
        path.parent / "import_manifest.json.tmp": b"{",
    }
    for p, data in {**keep, **remove}.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (folder / "link.jpg.a1b2c3d4e5f6.part").symlink_to(folder / "001-0123456789ab.jpg")   # ссылка — не трогать
    res, log = _run(path, uploads, apply=True, now=LATER)
    assert sorted(res.deleted) == sorted(remove) and res.verified
    assert all(p.read_bytes() == d for p, d in keep.items()) and (folder / "link.jpg.a1b2c3d4e5f6.part").is_symlink()
    assert "удалён" in log


def test_a_file_removed_by_someone_else_meanwhile_is_not_an_error(fake_faiss, tmp_path, monkeypatch):
    path = tmp_path / "idx" / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    part = path.parent / "uploads_manifest.json.a1b2c3d4e5f6.part"
    part.write_bytes(b"x")
    real_unlink = Path.unlink

    def gone_first(self, *a, **k):
        if self == part:
            real_unlink(self)
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", gone_first)
    res, log = _run(path, apply=True, now=LATER)
    assert res.errors == [] and "уже удалён" in log


def test_script_writes_a_log_file_and_returns_success(fake_faiss, tmp_path, monkeypatch):
    from scripts import cleanup_temp
    path, temps = _setup(fake_faiss, tmp_path, monkeypatch)
    settings = types.SimpleNamespace(matcher=types.SimpleNamespace(index_path=str(path)),
                                     embedder=types.SimpleNamespace(embedding_dim=3), media_root=str(tmp_path / "media"))
    monkeypatch.setattr(T.time, "time", lambda: LATER)
    lines = []
    code = cleanup_temp.main(["--apply"], out=lines.append, settings=settings, resolve_path=lambda p: Path(p),
                             log_dir=tmp_path / "logs")
    log_text = (tmp_path / "logs" / "cleanup-temp.log").read_text(encoding="utf-8")
    assert code == 0 and "удалено: 2" in log_text and "проверка результата: пройдена" in log_text
    assert not any(p.exists() for p in temps)
