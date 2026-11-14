"""Жизненный цикл временных кадров «Узнать товар» (app/services/frame_lifecycle.py) — временная мера D-10а, не её выполнение."""
from __future__ import annotations

import ast
import os
import threading
import time
from pathlib import Path

import pytest

from app.services.frame_lifecycle import FRAME_TTL_SECONDS, SWEEP_INTERVAL_SECONDS, PeriodicSweeper, sweep_frames

ROOT = Path(__file__).resolve().parents[1]


def _frame(folder: Path, name: str, age: float) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"\xff\xd8frame")
    os.utime(path, (time.time() - age, time.time() - age))
    return path


def test_ttl_and_interval_match_the_requirements():
    assert FRAME_TTL_SECONDS == 120          # ТК-2: не дольше двух минут
    assert SWEEP_INTERVAL_SECONDS <= 10      # документ 04, §4.2


def test_sweep_removes_only_frames_older_than_the_ttl(tmp_path):
    folder = tmp_path / "identify-tmp"
    old = _frame(folder, "a" * 32 + ".jpg", FRAME_TTL_SECONDS + 5)
    fresh = _frame(folder, "b" * 32 + ".jpg", FRAME_TTL_SECONDS - 20)
    assert sweep_frames(tmp_path) == 1
    assert not old.exists() and fresh.exists()


def test_sweep_does_not_touch_other_media_folders(tmp_path):
    photo = _frame(tmp_path / "catalog-uploads" / "SKU1", "p1.jpg", 10_000)
    assert sweep_frames(tmp_path) == 0 and photo.exists()


def test_sweep_of_a_missing_folder_is_harmless(tmp_path):
    assert sweep_frames(tmp_path / "nothing-here") == 0


def test_sweeper_runs_immediately_and_then_periodically():
    calls = []
    done = threading.Event()

    def sweep():
        calls.append(time.monotonic())
        if len(calls) >= 3:
            done.set()
        return 0

    sweeper = PeriodicSweeper(sweep, interval=0.02)
    sweeper.start()
    try:
        assert done.wait(2.0)
    finally:
        sweeper.stop()
    assert len(calls) >= 3 and not sweeper.alive


def test_one_failing_sweep_does_not_stop_the_next_ones():
    calls = {"n": 0}
    done = threading.Event()

    def sweep():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("диск занят")
        done.set()
        return 1

    sweeper = PeriodicSweeper(sweep, interval=0.02)
    sweeper.start()
    try:
        assert done.wait(2.0)
    finally:
        sweeper.stop()
    assert sweeper.failures == 1 and sweeper.runs >= 2


def test_stop_ends_the_thread_promptly_even_with_a_long_interval():
    sweeper = PeriodicSweeper(lambda: 0, interval=3600)
    sweeper.start()
    started = time.monotonic()
    sweeper.stop()
    assert not sweeper.alive and time.monotonic() - started < 2


def test_interval_must_be_positive():
    with pytest.raises(ValueError):
        PeriodicSweeper(lambda: 0, interval=0)


# --- подключение (FastAPI и Celery в облачной среде не установлены: проверяется исходный код) ---------------------------

def _tree(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"))


def test_api_sweeps_frames_on_startup_and_periodically_via_lifespan():
    tree = _tree("app/main.py")
    lifespan = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    src = ast.unparse(lifespan)
    assert "PeriodicSweeper" in src and "sweep_frames" in src and ".start()" in src and ".stop()" in src
    assert "finally" in src                                             # уборщик останавливается и при ошибке запуска
    fastapi_call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "FastAPI")
    assert any(k.arg == "lifespan" and getattr(k.value, "id", "") == "lifespan" for k in fastapi_call.keywords)


def test_worker_sweeps_stale_frames_when_it_starts():
    tree = _tree("app/worker.py")
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_sweep_stale_frames")
    decorators = [ast.unparse(d) for d in fn.decorator_list]
    assert "worker_ready.connect" in decorators and "sweep_frames" in ast.unparse(fn)
    assert any(isinstance(n, ast.Try) for n in ast.walk(fn))            # сбой уборки не роняет запуск воркера


def test_identify_route_queues_with_expiry_and_never_with_plain_delay():
    src = (ROOT / "app/api/routes_catalog.py").read_text(encoding="utf-8")
    route = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "run_identify")
    body = ast.unparse(route)
    assert "identify_product_task.apply_async(args=[frame_ref(frame_id)], expires=IDENTIFY_WAIT_SECONDS)" in body
    assert "save_identify_frame" not in src                     # D-10а: API больше не пишет кадр файлом
    assert "identify_product_task.delay" not in body
