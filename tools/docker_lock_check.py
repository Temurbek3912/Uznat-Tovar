"""Проверка блокировки индекса на общей папке Docker Desktop (Windows) настоящими процессами.

Запуск (на компьютере с Docker Desktop; порты не нужны, рабочие контейнеры не трогаются):
    .venv/Scripts/python.exe tools/docker_lock_check.py [--image retail-shelf-audit-api:latest] [--dir D:/AI-cache/lockcheck]

Временные контейнеры (`docker run --rm`, без портов) монтируют код только для чтения и пустую папку для блокировок, затем
берут `IndexLock` (app/pipeline/index_store.py) так же, как воркер и скрипты:
  0. что отвечает `flock` на общей папке (работает / ENOLCK / ошибка);
  1. контейнер держит блокировку — другой контейнер должен ждать (BUSY);
  2. контейнер держит — Windows-Python на хосте пробует;
  3. Windows-Python держит — контейнер пробует.
Ожидаемое (проверено 2026-11-21 на Docker Desktop WSL2): 0 — работает, 1 — BUSY, 2 и 3 — блокировки друг друга НЕ видят
(разные ядра), поэтому скрипты индекса при программе в Docker запускать внутри контейнера.
Код выхода 0 — итог совпал с ожидаемым; 1 — нет (подробности в выводе).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HOLD = """
import os, sys, time
sys.path.insert(0, os.environ["CODE"])
from app.pipeline.index_store import IndexLock
with IndexLock(os.path.join(os.environ["LOCKDIR"], "catalog.faiss")) as h:
    print("locked" if h.locked else "unsupported", flush=True)
    time.sleep(float(os.environ.get("HOLD_SECONDS", "8")))
"""

TRY = """
import os, sys
sys.path.insert(0, os.environ["CODE"])
from app.pipeline.index_store import IndexBusyError, IndexLock
try:
    with IndexLock(os.path.join(os.environ["LOCKDIR"], "catalog.faiss"), timeout=1) as h:
        print("free" if h.locked else "unsupported", flush=True)
except IndexBusyError:
    print("busy", flush=True)
"""

RAW = """
import errno, fcntl
f = open("/shared/raw.lock", "a+b")
try:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("works")
except OSError as e:
    print("error", errno.errorcode.get(e.errno, e.errno))
"""


def _docker(image: str, shared: Path, code: str, *, background: bool = False):
    cmd = ["docker", "run", "--rm", "--entrypoint", "python", "-v", f"{ROOT}:/app:ro", "-v", f"{shared}:/shared",
           "-e", "CODE=/app", "-e", "LOCKDIR=/shared", "-e", "PYTHONDONTWRITEBYTECODE=1", image, "-c", code]
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    if background:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=env)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace", env=env)
    return _last_line(out.stdout) or f"error: {out.stderr.strip()[-300:]}"


def _host(shared: Path, code: str, *, background: bool = False):
    env = dict(os.environ, CODE=str(ROOT), LOCKDIR=str(shared), PYTHONDONTWRITEBYTECODE="1")
    cmd = [sys.executable, "-c", code]
    if background:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=env)
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace", env=env)
    return _last_line(out.stdout) or f"error: {out.stderr.strip()[-300:]}"


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _held_then_try(holder, trier) -> tuple[str, str]:
    proc = holder()
    try:
        first = proc.stdout.readline().strip()        # ждём, пока держатель возьмёт блокировку (контейнер стартует не сразу)
        if first != "locked":
            return first or "holder failed", "-"
        return first, trier()
    finally:
        proc.wait(60)
        proc.stdout.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # русский текст в консоли Windows
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", default="retail-shelf-audit-api:latest", help="образ с Python и зависимостями программы")
    parser.add_argument("--dir", default=None, help="папка для файлов блокировки (по умолчанию — временная рядом с проектом)")
    args = parser.parse_args()
    if shutil.which("docker") is None:
        print("docker не найден")
        return 1
    shared = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="lockcheck-", dir=ROOT.parent))
    shared.mkdir(parents=True, exist_ok=True)
    print(f"Папка блокировок: {shared}; образ: {args.image}")

    results = {}
    results["0 flock на общей папке"] = (_docker(args.image, shared, RAW), "works")
    results["1 контейнер держит, контейнер пробует"] = (_held_then_try(
        lambda: _docker(args.image, shared, HOLD, background=True), lambda: _docker(args.image, shared, TRY))[1], "busy")
    time.sleep(1)
    results["2 контейнер держит, Windows пробует"] = (_held_then_try(
        lambda: _docker(args.image, shared, HOLD, background=True), lambda: _host(shared, TRY))[1], "free")
    results["3 Windows держит, контейнер пробует"] = (_held_then_try(
        lambda: _host(shared, HOLD, background=True), lambda: _docker(args.image, shared, TRY))[1], "free")

    ok = True
    for name, (got, expected) in results.items():
        mark = "как ожидалось" if got == expected else "НЕ КАК ОЖИДАЛОСЬ"
        ok &= got == expected
        print(f"{name}: {got} (ожидалось {expected}) — {mark}")
    if not args.dir:
        shutil.rmtree(shared, ignore_errors=True)
    print("Итог: совпадает с проверкой 2026-11-21" if ok else "Итог: поведение отличается — см. docs/ENGINEERING_STATUS.md §3")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
