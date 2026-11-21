"""Проверка блокировки индекса на общей папке Docker Desktop (Windows) настоящими процессами.

Запуск (на компьютере с Docker Desktop; порты не нужны, рабочие контейнеры не трогаются):
    .venv/Scripts/python.exe tools/docker_lock_check.py [--image retail-shelf-audit-api:latest] [--dir D:/AI-cache/lockcheck]

Временные контейнеры (`docker run --rm`, без портов) монтируют код только для чтения и пустую папку для блокировок, затем
берут `IndexLock` (app/pipeline/index_store.py) так же, как воркер и скрипты:
  0. что отвечает `flock` на общей папке (работает / ENOLCK / ошибка);
  1. контейнер держит блокировку — другой контейнер должен ждать (BUSY);
  2. контейнер держит — Windows-Python на хосте пробует;
  3. Windows-Python держит — контейнер пробует;
  4. два контейнера одновременно пишут общий индекс (open_catalog_index → add → save, повтор при IndexChangedError, как воркер
     и импорт): все записи на месте, без потерь и повторов, каждый вектор под своим id;
  5. то же, но блокировка выключена (как на файловой системе без блокировки) — показывает, что остаётся от защиты одной
     проверкой поколения; итог этой строки — сведения, не ошибка.
Шаги 0, 1, 4 и 5 повторяются на временном именованном томе Docker (`docker volume create`, после проверки удаляется).
Ожидаемое (проверено 2026-11-21 на Docker Desktop WSL2): 0 — работает, 1 — BUSY, 2 и 3 — блокировки друг друга НЕ видят
(разные ядра), поэтому скрипты индекса при программе в Docker запускать внутри контейнера; 4 — без потерь; то же на томе.
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

WRITER = """
import errno, os, sys, time
sys.path.insert(0, os.environ["CODE"])
import numpy as np
from app.pipeline import index_store
from app.pipeline import matcher as M
if os.environ.get("NOLOCK") == "1":               # как файловая система без блокировки: IndexLock работает без неё
    def _no_lock(fh):
        raise OSError(errno.ENOLCK, "lock disabled for the check")
    index_store._try_lock = _no_lock
worker, rounds, total = int(os.environ["WORKER"]), int(os.environ["ROUNDS"]), int(os.environ["WRITERS"])
folder = os.environ["LOCKDIR"]
path = os.path.join(folder, os.environ["INDEX_NAME"])
tag = os.environ["INDEX_NAME"]
open(os.path.join(folder, f"{tag}.ready-{worker}"), "w").close()
deadline = time.monotonic() + 120                 # ждём остальных: контейнеры стартуют не одновременно
while sum(os.path.exists(os.path.join(folder, f"{tag}.ready-{w}")) for w in range(total)) < total:
    if time.monotonic() > deadline:
        print("error: others not ready", flush=True)
        sys.exit(1)
    time.sleep(0.02)
conflicts = 0
try:
    for i in range(rounds):
        while True:
            index = M.open_catalog_index(path, 3)
            index.add(f"w{worker}-{i}", np.array([1.0, worker + 1.0, i + 1.0], dtype=np.float32) / 10)
            time.sleep(0.01)                      # как время работы модели между загрузкой и сохранением
            try:
                index.save(path)
                break
            except M.IndexChangedError:
                conflicts += 1
except Exception as exc:                          # причина — в итог проверки
    print(f"error {type(exc).__name__}: {str(exc)[:200]}", flush=True)
    sys.exit(1)
print(f"ok {conflicts}", flush=True)
"""

VERIFY = """
import os, sys
sys.path.insert(0, os.environ["CODE"])
import numpy as np
from app.pipeline import matcher as M
index = M.open_catalog_index(os.path.join(os.environ["LOCKDIR"], os.environ["INDEX_NAME"]), 3)
ids = list(index._ids)
if hasattr(index, "_vectors"):
    stored = np.stack(index._vectors) if index._vectors else np.zeros((0, 3), dtype=np.float32)
else:
    stored = index._index.reconstruct_n(0, index._index.ntotal)
wrong = abs(len(ids) - len(stored))
for pid, vec in zip(ids, stored):                 # вектор каждого товара — под его id (сравниваем направление)
    w, i = (int(x) for x in pid[1:].split("-"))
    want = np.array([1.0, w + 1.0, i + 1.0], dtype=np.float32)
    cos = float(vec @ want) / (float(np.linalg.norm(vec)) * float(np.linalg.norm(want)) or 1.0)
    wrong += cos < 0.9999
print(f"{len(ids)} {len(set(ids))} {wrong}", flush=True)
"""

WRITERS, ROUNDS = 2, 25


def _docker(image: str, shared: Path | str, code: str, *, background: bool = False, env: dict | None = None):
    extra = [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
    cmd = ["docker", "run", "--rm", "--entrypoint", "python", "-v", f"{ROOT}:/app:ro", "-v", f"{shared}:/shared",
           "-e", "CODE=/app", "-e", "LOCKDIR=/shared", "-e", "PYTHONDONTWRITEBYTECODE=1", *extra, image, "-c", code]
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


def _concurrent_writes(image: str, shared: Path | str, name: str, *, nolock: bool) -> str:
    """WRITERS контейнеров по ROUNDS раз добавляют товар и сохраняют общий индекс. «ok …», если все записи на месте и каждый
    вектор под своим id; иначе — сколько потеряно."""
    base = {"INDEX_NAME": name, "ROUNDS": ROUNDS, "WRITERS": WRITERS, "NOLOCK": "1" if nolock else "0"}
    procs = [_docker(image, shared, WRITER, background=True, env={**base, "WORKER": w}) for w in range(WRITERS)]
    outs = []
    for proc in procs:
        out, _ = proc.communicate(timeout=600)
        outs.append(_last_line(out))
    if not all(o.startswith("ok ") for o in outs):
        return f"writer failed: {outs}"
    conflicts = sum(int(o.split()[1]) for o in outs)
    got = _docker(image, shared, VERIFY, env={"INDEX_NAME": name})
    try:
        count, unique, wrong = (int(x) for x in got.split())
    except ValueError:
        return f"verify failed: {got}"
    expected = WRITERS * ROUNDS
    if count == expected and unique == expected and wrong == 0:
        return f"ok (записей {expected}, повторов из-за чужой записи {conflicts})"
    return f"потеряно {expected - unique} из {expected}, не под своим id {wrong}, повторов {conflicts}"


def _passed(got: str, expected: str) -> bool:
    return got == expected or (expected == "ok" and got.startswith("ok "))


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
    results["4 два контейнера пишут индекс"] = (_concurrent_writes(args.image, shared, "w-lock.faiss", nolock=False), "ok")
    info = {"5 без блокировки (сведения)": _concurrent_writes(args.image, shared, "w-nolock.faiss", nolock=True)}

    volume = f"lockcheck-{os.getpid()}"                                   # временный именованный том, удаляется ниже
    subprocess.run(["docker", "volume", "create", volume], capture_output=True, check=True)
    try:
        results["0в flock на именованном томе"] = (_docker(args.image, volume, RAW), "works")
        results["1в том: контейнер держит, контейнер пробует"] = (_held_then_try(
            lambda: _docker(args.image, volume, HOLD, background=True), lambda: _docker(args.image, volume, TRY))[1], "busy")
        results["4в том: два контейнера пишут индекс"] = (
            _concurrent_writes(args.image, volume, "w-lock.faiss", nolock=False), "ok")
        info["5в том: без блокировки (сведения)"] = _concurrent_writes(args.image, volume, "w-nolock.faiss", nolock=True)
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)

    ok = True
    for name, (got, expected) in results.items():
        passed = _passed(got, expected)
        ok &= passed
        print(f"{name}: {got} (ожидалось {expected}) — {'как ожидалось' if passed else 'НЕ КАК ОЖИДАЛОСЬ'}")
    for name, got in info.items():
        print(f"{name}: {got}")
    if not args.dir:
        shutil.rmtree(shared, ignore_errors=True)
    print("Итог: совпадает с проверкой 2026-11-21" if ok else "Итог: поведение отличается — см. docs/ENGINEERING_STATUS.md §3")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
