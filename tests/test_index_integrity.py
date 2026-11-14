"""
Целостность индекса каталога (app/pipeline/matcher.py, 2026-11-07).

Индекс — единственная копия эталонных векторов. Раньше:
  - повреждённый файл индекса (или ошибка чтения) молча заменялся ПУСТЫМ индексом, и первое же добавление товара записывало
    его поверх файла — все эталоны терялись;
  - без файла id или при расхождении числа id и векторов поиск падал или возвращал ЧУЖИЕ товары;
  - запись шла прямо в итоговые файлы: обрыв посреди записи портил индекс;
  - воркер со старой копией в памяти затирал индекс, только что записанный импортом.
Эти тесты воспроизводят каждый случай. FAISS заменён маленькой поддельной библиотекой с тем же интерфейсом (настоящего faiss-cpu
в среде тестов может не быть); подмена делается через monkeypatch и снимается после каждого теста.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import matcher as M

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- поддельный faiss

class _FakeFlatIP:
    def __init__(self, d: int):
        self.d = d
        self._v = np.zeros((0, d), dtype=np.float32)

    @property
    def ntotal(self) -> int:
        return int(self._v.shape[0])

    def add(self, x):
        x = np.asarray(x, dtype=np.float32)
        assert x.ndim == 2 and x.shape[1] == self.d, "размерность не совпадает (настоящий FAISS тоже падает)"
        self._v = np.vstack([self._v, x])

    def reconstruct_n(self, start, n):
        return self._v[start:start + n].copy()

    def search(self, q, k):
        sims = np.asarray(q, dtype=np.float32) @ self._v.T
        idx = np.argsort(-sims, axis=1)[:, :k]
        return np.take_along_axis(sims, idx, axis=1), idx


_MAGIC = b"FAKEFAISS1"


def _write_index(index: _FakeFlatIP, path: str) -> None:
    with open(path, "wb") as fh:
        fh.write(_MAGIC + struct.pack("<ii", index.d, index.ntotal) + index._v.astype("<f4").tobytes())


def _read_index(path: str) -> _FakeFlatIP:
    return _deserialize_index(np.frombuffer(Path(path).read_bytes(), dtype=np.uint8))


def _deserialize_index(arr) -> _FakeFlatIP:
    data = np.asarray(arr, dtype=np.uint8).tobytes()
    if not data.startswith(_MAGIC) or len(data) < len(_MAGIC) + 8:
        raise RuntimeError("Error in read_index: bad magic")
    d, n = struct.unpack("<ii", data[len(_MAGIC):len(_MAGIC) + 8])
    body = data[len(_MAGIC) + 8:]
    if len(body) != d * n * 4:
        raise RuntimeError("Error in read_index: truncated")
    index = _FakeFlatIP(d)
    if n:
        index._v = np.frombuffer(body, dtype="<f4").reshape(n, d).copy()
    return index


def _make_fake_faiss() -> types.ModuleType:
    mod = types.ModuleType("faiss")
    mod.IndexFlatIP = _FakeFlatIP
    mod.write_index = _write_index
    mod.read_index = _read_index
    mod.deserialize_index = _deserialize_index
    return mod


@pytest.fixture
def fake_faiss(monkeypatch):
    mod = _make_fake_faiss()
    monkeypatch.setitem(sys.modules, "faiss", mod)
    return mod


def _unit(*v: float) -> np.ndarray:
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def _faiss_index(fake_faiss, items=(("cola", (1, 0, 0)), ("chips", (0, 0, 1)))) -> M.FaissCatalogIndex:
    index = M.FaissCatalogIndex(embedding_dim=3)
    for pid, vec in items:
        index.add(pid, _unit(*vec))
    return index


def _files(path: Path) -> dict[str, bytes]:
    """Содержимое папки индекса без файла блокировки (он пустой и остаётся намеренно)."""
    return {p.name: p.read_bytes() for p in sorted(path.parent.iterdir()) if not p.name.endswith(".lock")}


# --------------------------------------------------------------------------- FAISS: формат и проверки при загрузке

def test_faiss_roundtrip_writes_checksummed_sidecar_and_searches_the_same(fake_faiss, tmp_path):
    path = tmp_path / "idx" / "catalog.faiss"
    original = _faiss_index(fake_faiss)
    original.save(str(path))
    meta = json.loads((tmp_path / "idx" / "catalog.faiss.ids.json").read_text(encoding="utf-8"))
    assert meta["format"] == 2 and meta["count"] == 2 and meta["dim"] == 3 and meta["ids"] == ["cola", "chips"]
    assert isinstance(meta["generation"], str) and len(meta["generation"]) == 32
    assert meta["index_sha256"] == M._sha256_file(path)
    loaded = M.FaissCatalogIndex.load(str(path))
    q = _unit(1, 0.1, 0)
    assert loaded.search(q, 2) == original.search(q, 2)
    assert sorted(_files(path)) == ["catalog.faiss", "catalog.faiss.ids.json"], "временные файлы остались"


def test_missing_ids_file_is_refused_instead_of_crashing_or_mislabeling(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    (tmp_path / "catalog.faiss.ids.json").unlink()
    with pytest.raises(M.IndexIntegrityError, match="нет файла id"):
        M.FaissCatalogIndex.load(str(path))


def test_legacy_list_sidecar_still_loads(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _write_index(_faiss_index(fake_faiss)._index, str(path))
    (tmp_path / "catalog.faiss.ids.json").write_text(json.dumps(["cola", "chips"]), encoding="utf-8")
    loaded = M.FaissCatalogIndex.load(str(path))
    assert loaded.search(_unit(0, 0, 1), 1)[0][0] == "chips"


def test_legacy_sidecar_with_wrong_count_is_refused_not_silently_shifted(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _write_index(_faiss_index(fake_faiss)._index, str(path))
    (tmp_path / "catalog.faiss.ids.json").write_text(json.dumps(["cola"]), encoding="utf-8")
    with pytest.raises(M.IndexIntegrityError, match="не согласован"):
        M.FaissCatalogIndex.load(str(path))


def test_index_and_sidecar_from_different_saves_are_refused(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    old_sidecar = (tmp_path / "catalog.faiss.ids.json").read_bytes()
    _faiss_index(fake_faiss, (("fanta", (0, 1, 0)), ("water", (1, 1, 0)))).save(str(path))
    (tmp_path / "catalog.faiss.ids.json").write_bytes(old_sidecar)          # тот же размер списка, но чужие id
    with pytest.raises(M.IndexIntegrityError, match="контрольная сумма"):
        M.FaissCatalogIndex.load(str(path))


def test_corrupt_index_file_raises_integrity_error(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    path.write_bytes(b"garbage")
    (tmp_path / "catalog.faiss.ids.json").write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(M.IndexIntegrityError, match="не читается"):
        M.FaissCatalogIndex.load(str(path))


def test_save_refuses_inconsistent_in_memory_state(fake_faiss, tmp_path):
    index = _faiss_index(fake_faiss)
    index._ids.append("ghost")
    with pytest.raises(M.IndexIntegrityError):
        index.save(str(tmp_path / "catalog.faiss"))
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- атомарность записи

def test_crash_while_writing_keeps_the_previous_files_and_no_temp_files(fake_faiss, tmp_path, monkeypatch):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    before = _files(path)

    def broken_write(index, p):
        Path(p).write_bytes(b"FAKEFAISS1 partial")
        raise OSError("диск заполнен")

    monkeypatch.setattr(fake_faiss, "write_index", broken_write)
    bigger = _faiss_index(fake_faiss, (("cola", (1, 0, 0)), ("chips", (0, 0, 1)), ("water", (0, 1, 0))))
    with pytest.raises(OSError):
        bigger.save(str(path))
    assert _files(path) == before
    assert M.FaissCatalogIndex.load(str(path)).search(_unit(1, 0, 0), 1)[0][0] == "cola"


class _ProcessDied(BaseException):
    """Имитация гибели процесса: не Exception, поэтому никакой обработчик в коде её не «лечит»."""


def _die_on_replace(monkeypatch, nth: int, exc: BaseException):
    real_replace, calls = os.replace, []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) == nth:
            raise exc
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    return real_replace


@pytest.mark.parametrize("exc", [_ProcessDied(), OSError("ошибка записи")])
def test_crash_between_the_two_replacements_is_completed_on_next_load(fake_faiss, tmp_path, monkeypatch, exc):
    """Новый индекс уже заменил старый, а список id — нет. Загрузка должна завершить замену по временному списку с контрольной
    суммой ТЕКУЩЕГО индекса — и вернуть новые товары, а не отказать и тем более не смешать старые id с новыми векторами."""
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    real_replace = _die_on_replace(monkeypatch, 2, exc)
    with pytest.raises((type(exc), M.IndexIntegrityError)):
        _faiss_index(fake_faiss, (("fanta", (0, 1, 0)), ("water", (1, 1, 0)))).save(str(path))
    monkeypatch.setattr(os, "replace", real_replace)
    assert any(".ids.json.tmp-" in n for n in _files(path)), "временный список id должен остаться для завершения"
    loaded = M.FaissCatalogIndex.load(str(path))
    assert loaded._ids == ["fanta", "water"]
    assert loaded.search(_unit(0, 1, 0), 1) == [("fanta", pytest.approx(1.0))]
    assert sorted(_files(path)) == ["catalog.faiss", "catalog.faiss.ids.json"]


def test_first_ever_save_interrupted_between_replacements_is_completed(fake_faiss, tmp_path, monkeypatch):
    path = tmp_path / "catalog.faiss"
    real_replace = _die_on_replace(monkeypatch, 2, _ProcessDied())
    with pytest.raises(_ProcessDied):
        _faiss_index(fake_faiss).save(str(path))
    monkeypatch.setattr(os, "replace", real_replace)
    assert M.open_catalog_index(str(path), 3)._ids == ["cola", "chips"]


def test_crash_before_any_replacement_keeps_the_old_pair(fake_faiss, tmp_path, monkeypatch):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    real_replace = _die_on_replace(monkeypatch, 1, _ProcessDied())
    with pytest.raises(_ProcessDied):
        _faiss_index(fake_faiss, (("fanta", (0, 1, 0)),)).save(str(path))
    monkeypatch.setattr(os, "replace", real_replace)
    assert M.FaissCatalogIndex.load(str(path))._ids == ["cola", "chips"]


def test_roll_forward_ignores_a_temp_list_written_for_another_index(fake_faiss, tmp_path):
    """Временный список от другой записи (другая контрольная сумма) не должен быть поставлен на место."""
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    foreign = json.loads((tmp_path / "catalog.faiss.ids.json").read_text(encoding="utf-8"))
    _faiss_index(fake_faiss, (("fanta", (0, 1, 0)), ("water", (1, 1, 0)))).save(str(path))
    (tmp_path / "catalog.faiss.ids.json").write_text(json.dumps(foreign), encoding="utf-8")       # чужой список на месте
    (tmp_path / "catalog.faiss.ids.json.tmp-1-deadbeef").write_text(json.dumps(foreign), encoding="utf-8")
    with pytest.raises(M.IndexIntegrityError, match="контрольная сумма"):
        M.FaissCatalogIndex.load(str(path))


def test_index_replaced_by_another_process_during_load_never_mixes_ids_and_vectors(fake_faiss, tmp_path, monkeypatch):
    """Гонка, которую пропускала первая версия исправления: контрольная сумма считалась по одному чтению файла, а индекс
    разбирался из другого. Здесь файл индекса подменяется (тем же числом векторов, другими товарами) сразу после того, как
    загрузка прочла байты. Результат обязан соответствовать списку id, прочитанному вместе с ними."""
    path = tmp_path / "catalog.faiss"
    other = tmp_path / "other" / "catalog.faiss"
    _faiss_index(fake_faiss, (("fanta", (0, 1, 0)), ("water", (1, 1, 0)))).save(str(other))
    _faiss_index(fake_faiss).save(str(path))
    real_sha, swapped = M.sha256_bytes, []

    def sha_then_swap(data):
        if not swapped:
            swapped.append(True)
            os.replace(other, path)                       # другой процесс заменил файл индекса
        return real_sha(data)

    monkeypatch.setattr(M, "sha256_bytes", sha_then_swap)
    loaded = M.FaissCatalogIndex.load(str(path))
    assert swapped
    assert loaded._ids == ["cola", "chips"]
    assert loaded.search(_unit(1, 0, 0), 1) == [("cola", pytest.approx(1.0))]       # вектор «колы» — у «колы»


def test_without_deserialize_index_a_swap_during_load_is_detected(fake_faiss, tmp_path, monkeypatch):
    monkeypatch.delattr(fake_faiss, "deserialize_index")
    path = tmp_path / "catalog.faiss"
    other = tmp_path / "other" / "catalog.faiss"
    _faiss_index(fake_faiss, (("fanta", (0, 1, 0)), ("water", (1, 1, 0)))).save(str(other))
    _faiss_index(fake_faiss).save(str(path))
    real_read, swapped = fake_faiss.read_index, []

    def swap_then_read(p):
        if not swapped:
            swapped.append(True)
            os.replace(other, path)
        return real_read(p)

    monkeypatch.setattr(fake_faiss, "read_index", swap_then_read)
    with pytest.raises(M.IndexIntegrityError):
        M.FaissCatalogIndex.load(str(path))


def test_disk_full_while_writing_the_id_list_keeps_the_old_pair(fake_faiss, tmp_path, monkeypatch):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    before = _files(path)
    real_dump = json.dump

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(M.json, "dump", full_disk)
    with pytest.raises(OSError):
        _faiss_index(fake_faiss, (("fanta", (0, 1, 0)),)).save(str(path))
    monkeypatch.setattr(M.json, "dump", real_dump)
    assert _files(path) == before


def test_file_held_open_by_antivirus_is_retried(fake_faiss, tmp_path, monkeypatch):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    real_replace, refusals = os.replace, [2]

    def busy_twice(src, dst):
        if refusals[0]:
            refusals[0] -= 1
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", busy_twice)
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    index = M.FaissCatalogIndex.load(str(path))
    index.add("fanta", _unit(0, 1, 0))
    index.save(str(path))
    monkeypatch.setattr(os, "replace", real_replace)
    assert M.FaissCatalogIndex.load(str(path))._ids == ["cola", "chips", "fanta"]


def test_index_file_locked_for_good_keeps_the_old_pair(fake_faiss, tmp_path, monkeypatch):
    from app.pipeline import index_store
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    before = _files(path)

    def always_busy(src, dst):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(os, "replace", always_busy)
    monkeypatch.setattr(index_store.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        _faiss_index(fake_faiss, (("fanta", (0, 1, 0)),)).save(str(path))
    assert _files(path) == before


def test_id_list_locked_for_good_after_the_index_was_replaced_is_completed_later(fake_faiss, tmp_path, monkeypatch):
    from app.pipeline import index_store
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    real_replace = os.replace

    def sidecar_busy(src, dst):
        if str(dst).endswith(".ids.json"):
            raise PermissionError(13, "locked")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", sidecar_busy)
    monkeypatch.setattr(index_store.time, "sleep", lambda s: None)
    with pytest.raises(M.IndexIntegrityError, match="завершена автоматически"):
        _faiss_index(fake_faiss, (("fanta", (0, 1, 0)),)).save(str(path))
    monkeypatch.setattr(os, "replace", real_replace)
    assert M.FaissCatalogIndex.load(str(path))._ids == ["fanta"]


# --------------------------------------------------------------------------- open_catalog_index: никогда не пустой индекс вместо файла

def test_missing_file_gives_a_new_empty_index(fake_faiss, tmp_path):
    index = M.open_catalog_index(str(tmp_path / "catalog.faiss"), 3)
    assert isinstance(index, M.FaissCatalogIndex) and index._index.ntotal == 0


def test_corrupt_file_is_never_replaced_by_an_empty_index(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    path.write_bytes(path.read_bytes()[:-4])                                  # обрезанный файл
    before = _files(path)
    with pytest.raises(M.IndexIntegrityError):
        M.open_catalog_index(str(path), 3)
    assert _files(path) == before, "файл индекса не должен меняться"


def test_existing_faiss_file_without_faiss_installed_is_refused(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "faiss", None)                           # import faiss -> ImportError
    path = tmp_path / "catalog.faiss"
    path.write_bytes(b"something")
    with pytest.raises(RuntimeError, match="faiss"):
        M.open_catalog_index(str(path), 3)


def test_dimension_mismatch_with_the_embedder_is_refused(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    with pytest.raises(M.IndexIntegrityError, match="размерности 3"):
        M.open_catalog_index(str(path), 768)


def test_stale_process_does_not_overwrite_an_index_written_by_another(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    worker = M.open_catalog_index(str(path), 3)                               # воркер загрузил индекс
    importer = M.open_catalog_index(str(path), 3)                             # импорт при работающем воркере
    importer.add("water", _unit(0, 1, 0))
    importer.save(str(path))
    after_import = _files(path)
    worker.add("fanta", _unit(1, 1, 0))
    with pytest.raises(M.IndexIntegrityError, match="изменён другой программой"):
        worker.save(str(path))
    assert _files(path) == after_import
    assert {pid for pid, _ in M.FaissCatalogIndex.load(str(path)).search(_unit(0, 1, 0), 3)} == {"cola", "chips", "water"}


def test_own_consecutive_saves_are_allowed(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    index = M.open_catalog_index(str(path), 3)                                # файла ещё нет
    for n, pid in enumerate(["a", "b", "c"]):
        index.add(pid, _unit(1, n, 0))
        index.save(str(path))
    assert M.FaissCatalogIndex.load(str(path))._ids == ["a", "b", "c"]


def test_file_created_by_someone_else_after_opening_an_absent_index_is_not_overwritten(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    worker = M.open_catalog_index(str(path), 3)
    _faiss_index(fake_faiss).save(str(path))                                  # импорт создал файл
    worker.add("x", _unit(1, 0, 0))
    with pytest.raises(M.IndexIntegrityError):
        worker.save(str(path))


def test_full_rebuild_with_a_directly_created_index_may_overwrite(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    rebuilt = _faiss_index(fake_faiss, (("water", (0, 1, 0)),))               # как ml/build_catalog_embeddings.py
    rebuilt.save(str(path))
    assert M.FaissCatalogIndex.load(str(path))._ids == ["water"]


# --------------------------------------------------------------------------- numpy-бэкенд

def test_numpy_index_is_saved_without_pickle(tmp_path):
    index = M.NumpyCatalogIndex()
    index.add("cola", _unit(1, 0, 0))
    index.save(str(tmp_path / "catalog"))
    with np.load(tmp_path / "catalog.npz", allow_pickle=False) as data:      # не бросает: объектных массивов нет
        assert list(data["ids"]) == ["cola"] and len(str(data["generation"])) == 32
    assert sorted(_files(tmp_path / "catalog.npz")) == ["catalog.npz"]


def test_numpy_legacy_pickled_file_still_loads(tmp_path):
    np.savez(tmp_path / "catalog.npz", ids=np.array(["cola", "chips"], dtype=object),
             vectors=np.stack([_unit(1, 0, 0), _unit(0, 0, 1)]))
    loaded = M.NumpyCatalogIndex.load(str(tmp_path / "catalog"))
    assert loaded.search(_unit(0, 0, 1), 1)[0][0] == "chips"


def test_numpy_count_mismatch_and_garbage_are_refused(tmp_path):
    np.savez(tmp_path / "a.npz", ids=np.array(["x"], dtype=str), vectors=np.stack([_unit(1, 0, 0), _unit(0, 1, 0)]))
    with pytest.raises(M.IndexIntegrityError):
        M.NumpyCatalogIndex.load(str(tmp_path / "a"))
    (tmp_path / "b.npz").write_bytes(b"not a zip")
    with pytest.raises(M.IndexIntegrityError):
        M.NumpyCatalogIndex.load(str(tmp_path / "b"))


def test_numpy_stale_guard_works_too(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "faiss", None)
    path = tmp_path / "catalog.faiss"
    a = M.open_catalog_index(str(path), 3)
    b = M.open_catalog_index(str(path), 3)
    assert isinstance(a, M.NumpyCatalogIndex)
    b.add("x", _unit(1, 0, 0))
    b.save(str(path))
    a.add("y", _unit(0, 1, 0))
    with pytest.raises(M.IndexIntegrityError):
        a.save(str(path))


# --------------------------------------------------------------------------- все загрузчики идут через open_catalog_index

def _function_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(f"def {name}(")
    nxt = text.find("\ndef ", start + 1)
    return text[start:nxt if nxt != -1 else len(text)]


@pytest.mark.parametrize("rel, name", [("app/worker.py", "_load_or_create_index"),
                                       ("scripts/add_product_from_local_file.py", "_load_index")])
def test_loaders_delegate_to_the_strict_opener_without_an_empty_fallback(rel, name):
    src = _function_source(ROOT / rel, name)
    assert "open_catalog_index(" in src
    for forbidden in ("FaissCatalogIndex(", "NumpyCatalogIndex(", "except RuntimeError", "except Exception"):
        assert forbidden not in src, f"{rel}:{name} снова подменяет индекс при ошибке ({forbidden})"


# --------------------------------------------------------------------------- одновременная запись несколькими процессами

def _writer_process(path: str, worker: int, rounds: int, use_numpy: bool, queue, barrier) -> None:
    import sys as _sys
    _sys.modules["faiss"] = None if use_numpy else _make_fake_faiss()      # отдельный процесс: своя подмена библиотеки
    from app.pipeline import matcher as MM
    import time as _time
    conflicts = 0
    try:
        barrier.wait(timeout=60)                      # все процессы начинают одновременно — иначе пересечений почти нет
        for i in range(rounds):
            while True:
                index = MM.open_catalog_index(path, 3)
                index.add(f"w{worker}-{i}", np.array([1.0, worker + 1.0, i + 1.0], dtype=np.float32) / 10)
                _time.sleep(0.01)                     # как время работы модели между загрузкой индекса и сохранением
                try:
                    index.save(path)
                    break
                except MM.IndexChangedError:
                    conflicts += 1
        queue.put(("ok", worker, conflicts))
    except BaseException as exc:  # noqa: BLE001 — причина уходит в тест
        queue.put(("error", worker, repr(exc)))


@pytest.mark.parametrize("use_numpy", [False, True], ids=["faiss", "numpy"])
def test_concurrent_writers_lose_nothing_and_never_mix_ids(fake_faiss, tmp_path, use_numpy):
    """Четыре настоящих процесса (как воркер и скрипты импорта) по 15 раз добавляют товар и сохраняют общий индекс. Итог: все
    60 товаров на месте, без потерь и повторов, и вектор каждого товара найден под его id."""
    import multiprocessing as mp
    ctx = mp.get_context("spawn")                     # не fork: fork многопоточного процесса может зависнуть
    path = str(tmp_path / "catalog.faiss")
    queue, barrier = ctx.Queue(), ctx.Barrier(4)
    procs = [ctx.Process(target=_writer_process, args=(path, w, 15, use_numpy, queue, barrier)) for w in range(4)]
    for p in procs:
        p.start()
    results = [queue.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=30)
    assert all(r[0] == "ok" for r in results), results
    if use_numpy:
        index = M.NumpyCatalogIndex.load(path)
    else:
        index = M.FaissCatalogIndex.load(path)
    expected = {f"w{w}-{i}" for w in range(4) for i in range(15)}
    assert sorted(index._ids) == sorted(expected)
    assert sum(r[2] for r in results) > 0, "процессы ни разу не пересеклись — тест ничего не проверил"
    stored = np.stack(index._vectors) if use_numpy else index._index._v
    for pid, vec in zip(index._ids, stored, strict=True):            # каждый вектор лежит под СВОИМ id
        w, i = (int(x) for x in pid[1:].split("-"))
        assert np.allclose(vec, np.array([1.0, w + 1.0, i + 1.0], dtype=np.float32) / 10), pid


def test_writer_waits_for_the_lock_and_gives_up_with_a_clear_error(fake_faiss, tmp_path, monkeypatch):
    from app.pipeline import index_store
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    index = M.FaissCatalogIndex.load(str(path))
    index.add("fanta", _unit(0, 1, 0))
    monkeypatch.setattr(index_store, "LOCK_TIMEOUT_SECONDS", 0.3)
    with index_store.IndexLock(path) as held:                 # другой процесс держит индекс
        assert held.locked, "эта файловая система не поддерживает flock — тест не имеет смысла"
        with pytest.raises(M.IndexBusyError):
            index.save(str(path))
    index.save(str(path))                                     # блокировка снята — сохранение проходит
    assert M.FaissCatalogIndex.load(str(path))._ids == ["cola", "chips", "fanta"]


# --------------------------------------------------------------------------- память воркера и диск

def test_worker_picks_up_an_index_written_by_an_import(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    matcher = M.ProductMatcher(M.open_catalog_index(str(path), 3))
    assert M.refresh_index_if_changed(matcher, str(path), 3) == "unchanged"
    importer = M.open_catalog_index(str(path), 3)
    importer.add("water", _unit(0, 1, 0))
    importer.save(str(path))
    assert M.refresh_index_if_changed(matcher, str(path), 3) == "reloaded"
    assert matcher.index._ids == ["cola", "chips", "water"]
    matcher.index.add("fanta", _unit(1, 1, 0))
    matcher.index.save(str(path))                            # после перечитывания сохранение снова разрешено
    assert M.FaissCatalogIndex.load(str(path))._ids == ["cola", "chips", "water", "fanta"]


def test_worker_keeps_its_copy_when_the_foreign_file_is_broken(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    matcher = M.ProductMatcher(M.open_catalog_index(str(path), 3))
    path.write_bytes(b"FAKEFAISS1 broken by someone")
    status = M.refresh_index_if_changed(matcher, str(path), 3)
    assert status.startswith("error:")
    assert matcher.index._ids == ["cola", "chips"]            # распознавание продолжает работать по прежней копии
    matcher.index.add("fanta", _unit(0, 1, 0))
    with pytest.raises(M.IndexChangedError):                  # но поверх чужого файла не пишет
        matcher.index.save(str(path))


def test_failed_save_leaves_unsaved_vectors_in_memory_and_resync_drops_them(fake_faiss, tmp_path, monkeypatch):
    """Задача добавила векторы в память, сохранение не удалось (диск полон) — база откатывается. Без пересинхронизации
    векторы-призраки остались бы в памяти и при следующем удачном сохранении попали бы на диск."""
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    matcher = M.ProductMatcher(M.open_catalog_index(str(path), 3))
    before = M.index_size(matcher.index)
    matcher.index.add("ghost-product", _unit(0, 1, 0))
    monkeypatch.setattr(fake_faiss, "write_index", lambda index, p: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(OSError):
        matcher.index.save(str(path))
    assert M.index_size(matcher.index) == before + 1          # вот он, призрак в памяти
    monkeypatch.setattr(fake_faiss, "write_index", _write_index)
    assert M.resync_index(matcher, str(path), 3) is None
    assert matcher.index._ids == ["cola", "chips"]


def test_resync_reports_an_unusable_disk_index(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    matcher = M.ProductMatcher(M.open_catalog_index(str(path), 3))
    (tmp_path / "catalog.faiss.ids.json").unlink()
    assert "IndexIntegrityError" in (M.resync_index(matcher, str(path), 3) or "")


def test_vectors_of_a_product_missing_in_the_database_are_never_returned(fake_faiss):
    """Призрак на диске (сохранение прошло, а commit базы — нет) не может стать ответом: поиск названий в базе его не находит."""
    from app.services.identify_service import ProductInfo, identify

    class _Embedder:
        def embed(self, images):
            return np.stack([_unit(1, 0.01, 0)])

    index = _faiss_index(fake_faiss, (("ghost", (1, 0, 0)), ("cola", (1, 0.05, 0))))
    matcher = M.ProductMatcher(index, match_threshold=0.9, unknown_threshold=0.5)
    result = identify(np.zeros((4, 4, 3), np.uint8), _Embedder(), matcher,
                      lambda ids: {i: ProductInfo(id=i, name="Кола") for i in ids if i == "cola"})
    assert result["product"]["id"] == "cola" and all(c["id"] != "ghost" for c in result["candidates"])


def test_adding_vectors_of_a_wrong_dimension_is_refused_before_touching_the_index(fake_faiss):
    index = _faiss_index(fake_faiss)
    with pytest.raises(ValueError):
        index.add("bad", np.ones(5, dtype=np.float32))
    assert M.index_size(index) == 2 and index._index.ntotal == 2


def test_foreign_save_interrupted_between_replacements_is_not_overwritten_but_completed(fake_faiss, tmp_path, monkeypatch):
    """Импорт заменил файл индекса и погиб до замены списка id. Список id на диске — ещё поколения воркера, поэтому одной
    сверки поколений мало: воркер обязан заметить смену самого файла индекса, не затирать его, а после перечитывания
    получить состояние импорта (замена списка завершается) и повторить своё добавление поверх."""
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    matcher = M.ProductMatcher(M.open_catalog_index(str(path), 3))
    importer = M.open_catalog_index(str(path), 3)
    importer.add("water", _unit(0, 1, 0))
    real_replace = _die_on_replace(monkeypatch, 2, _ProcessDied())
    with pytest.raises(_ProcessDied):
        importer.save(str(path))
    monkeypatch.setattr(os, "replace", real_replace)
    matcher.index.add("fanta", _unit(1, 1, 0))
    with pytest.raises(M.IndexChangedError):
        matcher.index.save(str(path))
    assert M.resync_index(matcher, str(path), 3) is None
    assert matcher.index._ids == ["cola", "chips", "water"]
    matcher.index.add("fanta", _unit(1, 1, 0))                 # повтор задачи
    matcher.index.save(str(path))
    assert M.FaissCatalogIndex.load(str(path))._ids == ["cola", "chips", "water", "fanta"]


# --------------------------------------------------------------------------- проводка в воркере (статически: Celery и SQLAlchemy
# в среде тестов может не быть) и защита от повторного добавления тех же фото

def _task_source(name: str) -> str:
    src = _function_source(ROOT / "app/worker.py", name)
    return src.split("\n@", 1)[0]                              # без декоратора следующей задачи


@pytest.mark.parametrize("task", ["add_catalog_product_task", "add_catalog_photos_task"])
def test_add_tasks_refresh_before_and_resync_in_every_failure_branch(task):
    import ast
    src = _task_source(task)
    assert src.index("_refresh_index()") < src.index("size_before = index_size(_matcher.index)") < src.index("db = SessionLocal()")
    tree = ast.parse(src)
    handlers = [h for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler)]
    outer = [h for h in handlers if ast.unparse(h).count("raise") >= 1 and "_ml_unavailable" not in ast.unparse(h)]
    assert outer, "нет обработчиков ошибок"
    for handler in outer:
        assert "_resync_index_after_failure(size_before" in ast.unparse(handler), ast.unparse(handler)[:200]


def test_identify_refreshes_the_index_before_searching():
    src = _task_source("identify_product_task")
    assert src.index("_refresh_index()") < src.index("identify(image, _embedder, _matcher, lookup)")


def test_photos_task_adds_only_photos_not_yet_in_the_index():
    src = _task_source("add_catalog_photos_task")
    assert "_not_indexed(index_path, product.id)(list(refs))" in src          # 2026-11-07, этап 4: решает сам индекс
    assert "_download_image(ref, digests) for ref in todo" in src and "index_photos(product.id, todo, images" in src
    assert "remember=_remember_photos(db, digests)" in src                   # 2026-11-14: фото и векторы — в базу (Р-6)


def test_resync_helper_marks_the_matcher_unready_when_the_disk_index_is_unusable():
    src = _function_source(ROOT / "app/worker.py", "_resync_index_after_failure")
    assert "resync_index(_matcher" in src and "ml_runtime.FAILED" in src and "write_status" in src
    assert "isinstance(exc, IndexIntegrityError)" in src


def test_not_yet_indexed_skips_recorded_upload_photos(tmp_path):
    from app.services.catalog_upload import not_yet_indexed, record_indexed
    index_path = tmp_path / "catalog.faiss"
    refs = ["upload:KOLA/001-a.jpg", "upload:KOLA/002-b.jpg"]
    assert not_yet_indexed(index_path, refs) == refs
    assert record_indexed(index_path, refs[:1])
    assert not_yet_indexed(index_path, refs + ["https://example.org/x.jpg"]) == ["upload:KOLA/002-b.jpg", "https://example.org/x.jpg"]
    (tmp_path / "uploads_manifest.json").write_text("{broken", encoding="utf-8")
    assert not_yet_indexed(index_path, refs) == refs          # журнал повреждён — лучше лишний вектор, чем пропущенное фото


def test_leftover_temp_files_of_an_interrupted_save_are_reported_not_deleted(fake_faiss, tmp_path, monkeypatch, caplog):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    real_replace = _die_on_replace(monkeypatch, 1, _ProcessDied())          # погиб до первой замены: временные файлы брошены
    monkeypatch.setattr(M, "discard", lambda p: None)                       # гибель процесса: уборки в except не было
    with pytest.raises(_ProcessDied):
        _faiss_index(fake_faiss, (("fanta", (0, 1, 0)),)).save(str(path))
    monkeypatch.setattr(os, "replace", real_replace)
    leftovers = [n for n in _files(path) if ".tmp-" in n]
    assert len(leftovers) == 2
    with caplog.at_level("WARNING", logger="app.pipeline.matcher"):
        assert M.FaissCatalogIndex.load(str(path))._ids == ["cola", "chips"]
    assert "временные файлы" in caplog.text and all(n in caplog.text for n in leftovers)
    assert [n for n in _files(path) if ".tmp-" in n] == leftovers           # ничего не удалено


# --------------------------------------------------------------------------- нестабильные отпечатки файлов (общие папки Docker
# Desktop на Windows: время изменения и номер файла могут меняться без изменения содержимого)

@pytest.fixture
def unstable_probes(monkeypatch):
    import itertools
    counter = itertools.count()
    real_probe = M.file_probe

    def jittery(path):
        base = real_probe(path)
        return None if base is None else (base[0], base[1], next(counter))      # «новый inode» при каждом вызове

    monkeypatch.setattr(M, "file_probe", jittery)


def test_unstable_probes_do_not_block_the_workers_own_saves(fake_faiss, tmp_path, unstable_probes):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    matcher = M.ProductMatcher(M.open_catalog_index(str(path), 3))
    for n in range(3):
        assert M.refresh_index_if_changed(matcher, str(path), 3) == "unchanged"     # без лишних перечитываний
        matcher.index.add(f"p{n}", _unit(1, n + 1, 0))
        matcher.index.save(str(path))
    assert M.FaissCatalogIndex.load(str(path))._ids == ["cola", "chips", "p0", "p1", "p2"]


def test_unstable_probes_still_detect_a_foreign_save(fake_faiss, tmp_path, unstable_probes):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    worker = M.open_catalog_index(str(path), 3)
    importer = M.open_catalog_index(str(path), 3)
    importer.add("water", _unit(0, 1, 0))
    importer.save(str(path))
    worker.add("fanta", _unit(1, 1, 0))
    with pytest.raises(M.IndexChangedError):
        worker.save(str(path))


def test_unstable_probes_and_a_foreign_index_file_with_our_old_id_list_are_detected(fake_faiss, tmp_path, monkeypatch, unstable_probes):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    worker = M.open_catalog_index(str(path), 3)
    importer = M.open_catalog_index(str(path), 3)
    importer.add("water", _unit(0, 1, 0))
    real_replace = _die_on_replace(monkeypatch, 2, _ProcessDied())
    with pytest.raises(_ProcessDied):
        importer.save(str(path))                                   # новый файл индекса на месте, список id — прежний
    monkeypatch.setattr(os, "replace", real_replace)
    worker.add("fanta", _unit(1, 1, 0))
    with pytest.raises(M.IndexChangedError):
        worker.save(str(path))


def test_legacy_index_can_be_saved_on_a_filesystem_with_unstable_probes(fake_faiss, tmp_path, unstable_probes):
    path = tmp_path / "catalog.faiss"
    _write_index(_faiss_index(fake_faiss)._index, str(path))
    (tmp_path / "catalog.faiss.ids.json").write_text(json.dumps(["cola", "chips"]), encoding="utf-8")
    index = M.open_catalog_index(str(path), 3)
    index.add("fanta", _unit(0, 1, 0))
    index.save(str(path))                                          # раньше: «изменён другой программой» из-за отпечатка
    assert M.FaissCatalogIndex.load(str(path))._ids == ["cola", "chips", "fanta"]


# --------------------------------------------------------------------------- этап 4: одна блокировка на индекс для всех бэкендов и
# всех процессов; источник (фото) у каждого вектора

@pytest.mark.parametrize("use_numpy", [False, True], ids=["faiss", "numpy"])
def test_every_backend_takes_the_same_lock_as_backup_and_cleanup(fake_faiss, tmp_path, monkeypatch, use_numpy):
    """Раньше numpy-индекс брал блокировку «catalog.faiss.npz.lock», а копия (сверка) и очистка — «catalog.faiss.lock»: очистка
    могла удалить временный файл, который в этот момент писал процесс без FAISS."""
    from app.pipeline import index_store
    if use_numpy:
        monkeypatch.setitem(sys.modules, "faiss", None)
    path = tmp_path / "catalog.faiss"
    index = M.open_catalog_index(str(path), 3)
    index.add("cola", _unit(1, 0, 0))
    monkeypatch.setattr(index_store, "LOCK_TIMEOUT_SECONDS", 0.2)
    with index_store.IndexLock(M.lock_target(str(path))) as held:      # так блокируют копия и очистка
        assert held.locked
        with pytest.raises(M.IndexBusyError):
            index.save(str(path))
    index.save(str(path))
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".lock")) == ["catalog.faiss.lock"]


@pytest.mark.parametrize("use_numpy", [False, True], ids=["faiss", "numpy"])
def test_vector_sources_survive_save_and_load_and_legacy_files_have_none(fake_faiss, tmp_path, monkeypatch, use_numpy):
    if use_numpy:
        monkeypatch.setitem(sys.modules, "faiss", None)
    path = tmp_path / "catalog.faiss"
    index = M.open_catalog_index(str(path), 3)
    index.add("cola", np.stack([_unit(1, 0, 0), _unit(1, 0.1, 0)]), ["upload:KOLA/001-aaaaaaaaaaaa.jpg", "import:" + "b" * 64])
    index.add("legacy", _unit(0, 1, 0))
    index.save(str(path))
    loaded = M.open_catalog_index(str(path), 3)
    assert loaded._sources == ["upload:KOLA/001-aaaaaaaaaaaa.jpg", "import:" + "b" * 64, None]
    with pytest.raises(ValueError):
        loaded.add("x", _unit(1, 1, 1), ["a", "b"])                     # источников больше, чем векторов
    assert M.index_size(loaded) == 3


def test_faiss_id_list_with_misaligned_sources_is_refused(fake_faiss, tmp_path):
    path = tmp_path / "catalog.faiss"
    _faiss_index(fake_faiss).save(str(path))
    sidecar = tmp_path / "catalog.faiss.ids.json"
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    meta["sources"] = ["upload:A/001-aaaaaaaaaaaa.jpg"]                  # 1 источник на 2 вектора
    sidecar.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(M.IndexIntegrityError, match="источников"):
        M.FaissCatalogIndex.load(str(path))


def _hold_lock(path: str, seconds: float, ready) -> None:
    from app.pipeline import index_store as IS
    from app.pipeline import matcher as MM
    import time as _t
    with IS.IndexLock(MM.lock_target(path)):
        ready.set()
        _t.sleep(seconds)


def test_lock_excludes_another_process_for_the_numpy_backend_too(tmp_path, monkeypatch):
    """Блокировка между ПРОЦЕССАМИ (как сверка в одном контейнере и запись в другом на одной ОС): пока другой процесс держит
    блокировку индекса, сохранение ждёт и сдаётся с понятной ошибкой."""
    import multiprocessing as mp
    from app.pipeline import index_store
    monkeypatch.setitem(sys.modules, "faiss", None)
    path = str(tmp_path / "catalog.faiss")
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(path, 2.0, ready))
    proc.start()
    try:
        assert ready.wait(30)
        index = M.open_catalog_index(path, 3)
        index.add("cola", _unit(1, 0, 0))
        monkeypatch.setattr(index_store, "LOCK_TIMEOUT_SECONDS", 0.3)
        with pytest.raises(M.IndexBusyError):
            index.save(path)
    finally:
        proc.join(30)
    index.save(path)
    assert M.NumpyCatalogIndex.load(path)._ids == ["cola"]
