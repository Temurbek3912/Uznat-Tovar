"""Блокировки и замена файлов индекса на настоящем Windows, другими процессами (не имитацией).

Что проверяется:
* `os.replace` поверх файла, который держит открытым ДРУГОЙ процесс (как антивирус или программа копирования), даёт
  `PermissionError` — даже если тот открыл файл со всеми разрешениями совместного доступа (FILE_SHARE_READ|WRITE|DELETE);
* `replace_with_retry` дожидается, пока файл отпустят, и заменяет его; если держат дольше всех попыток — ошибка наверх,
  временный файл и старый файл целы;
* сохранение индекса проходит, пока другой процесс ненадолго держит файл индекса открытым;
* `msvcrt.locking` (IndexLock) исключает другой процесс и снимается при его завершении, даже аварийном.

Только Windows: в Linux открытый файл не мешает замене.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="поведение замены и msvcrt.locking есть только в Windows")

# Дочерний процесс открывает файл через CreateFileW с заданным режимом совместного доступа, сообщает «open» и держит его.
_HOLDER = r"""
import ctypes, sys, time
from ctypes import wintypes
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.restype = wintypes.HANDLE
k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                          wintypes.DWORD, wintypes.HANDLE]
h = k.CreateFileW(sys.argv[1], 0x80000000, int(sys.argv[3]), None, 3, 0, None)   # GENERIC_READ, OPEN_EXISTING
if h in (None, wintypes.HANDLE(-1).value):
    print("fail", ctypes.get_last_error(), flush=True)
    sys.exit(1)
print("open", flush=True)
time.sleep(float(sys.argv[2]))
k.CloseHandle(wintypes.HANDLE(h))
"""

SHARE_READ, SHARE_ALL = 1, 7


def _finish(proc: subprocess.Popen) -> int:
    """Дождаться дочернего процесса и закрыть его канал вывода (иначе ResourceWarning)."""
    try:
        return proc.wait(30)
    finally:
        proc.stdout.close()


def _hold_open(path: Path, seconds: float, share: int = SHARE_ALL) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path), str(seconds), str(share)],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "open", "дочерний процесс не смог открыть файл"
    return proc


@pytest.mark.parametrize("share", [SHARE_READ, SHARE_ALL], ids=["share-read", "share-all"])
def test_open_handle_in_another_process_blocks_replace(tmp_path, share):
    dst, src = tmp_path / "catalog.faiss", tmp_path / "catalog.faiss.tmp"
    dst.write_bytes(b"old")
    src.write_bytes(b"new")
    proc = _hold_open(dst, 1.0, share)
    try:
        with pytest.raises(PermissionError):
            os.replace(src, dst)
    finally:
        _finish(proc)
    os.replace(src, dst)                                       # отпустили — замена проходит
    assert dst.read_bytes() == b"new"


def test_replace_with_retry_waits_until_the_other_process_lets_go(tmp_path, caplog):
    from app.pipeline.index_store import replace_with_retry
    dst, src = tmp_path / "catalog.faiss", tmp_path / "catalog.faiss.tmp"
    dst.write_bytes(b"old")
    src.write_bytes(b"new")
    proc = _hold_open(dst, 0.5)
    started = time.monotonic()
    try:
        with caplog.at_level("WARNING"):
            replace_with_retry(src, dst)
    finally:
        _finish(proc)
    assert dst.read_bytes() == b"new" and not src.exists()
    assert time.monotonic() - started >= 0.3                   # действительно ждали, а не прошли с первой попытки
    assert any("занят" in r.getMessage() for r in caplog.records)


def test_replace_with_retry_gives_up_and_keeps_both_files(tmp_path):
    from app.pipeline.index_store import replace_with_retry
    dst, src = tmp_path / "catalog.faiss", tmp_path / "catalog.faiss.tmp"
    dst.write_bytes(b"old")
    src.write_bytes(b"new")
    proc = _hold_open(dst, 3.0)
    try:
        with pytest.raises(PermissionError):
            replace_with_retry(src, dst, attempts=3, first_delay=0.1)      # 0,1 + 0,2 с — меньше, чем держат
    finally:
        _finish(proc)
    assert dst.read_bytes() == b"old" and src.read_bytes() == b"new"


def test_index_save_survives_the_index_file_being_held_for_a_moment(tmp_path, monkeypatch, caplog):
    from app.pipeline import matcher as M
    monkeypatch.setitem(sys.modules, "faiss", None)            # numpy-индекс: faiss в тестах не нужен
    path = str(tmp_path / "catalog.faiss")
    index = M.open_catalog_index(path, 3)
    index.add("cola", np.array([1.0, 0.0, 0.0], dtype=np.float32))
    index.save(path)
    held = Path(path + ".npz")                                 # numpy-индекс лежит в <индекс>.npz
    assert held.is_file()
    proc = _hold_open(held, 0.5)
    try:
        again = M.open_catalog_index(path, 3)
        again.add("fanta", np.array([0.0, 1.0, 0.0], dtype=np.float32))
        with caplog.at_level("WARNING"):
            again.save(path)
    finally:
        _finish(proc)
    assert any("занят" in r.getMessage() for r in caplog.records), "замена прошла сразу — файл не был занят"
    assert M.NumpyCatalogIndex.load(path)._ids == ["cola", "fanta"]


_LOCKER = r"""
import os, sys, time
sys.path.insert(0, sys.argv[3])
from app.pipeline.index_store import IndexLock
with IndexLock(sys.argv[1]) as held:
    print("locked" if held.locked else "unsupported", flush=True)
    if sys.argv[2] == "crash":
        os._exit(3)                                            # аварийное завершение, без __exit__
    time.sleep(float(sys.argv[2]))
"""


def _lock_in_child(target: Path, how: str) -> subprocess.Popen:
    root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.Popen([sys.executable, "-c", _LOCKER, str(target), how, root], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked", "msvcrt.locking не взялась в дочернем процессе"
    return proc


def test_msvcrt_lock_excludes_another_process_and_is_released_after(tmp_path):
    from app.pipeline.index_store import IndexBusyError, IndexLock
    target = tmp_path / "catalog.faiss"
    proc = _lock_in_child(target, "1.5")
    try:
        with pytest.raises(IndexBusyError):
            with IndexLock(target, timeout=0.3):
                pass
    finally:
        _finish(proc)
    with IndexLock(target, timeout=1) as held:                 # процесс закончил — блокировка свободна
        assert held.locked


def test_msvcrt_lock_is_released_when_the_holder_crashes(tmp_path):
    from app.pipeline.index_store import IndexLock
    target = tmp_path / "catalog.faiss"
    proc = _lock_in_child(target, "crash")
    assert _finish(proc) == 3
    with IndexLock(target, timeout=2) as held:                 # Windows снимает блокировку с закрытием дескриптора
        assert held.locked
