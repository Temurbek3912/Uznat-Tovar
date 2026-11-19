"""Восстановление индекса по расписанию (app/services/recovery_schedule.py, app/worker.py: _schedule_recovery).

Раньше recover_index ставился только при запуске воркера; теперь — и дальше, раз в recover_index_interval_minutes, в воркере
и в облегчённом режиме (в процессе API). Проверяется: расписание идёт, ошибки постановки его не останавливают, копии задачи
не копятся (expires = промежуток), второй запуск не создаёт второй поток, 0 — выключено.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.services.recovery_schedule import RecoveryTicker, interval_seconds

ROOT = Path(__file__).resolve().parent.parent


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_interval_from_setting_minutes():
    assert interval_seconds(30) == 1800
    assert interval_seconds("0.5") == 30
    assert interval_seconds(0) == 0 and interval_seconds(-5) == 0
    assert interval_seconds(None) == 0 and interval_seconds("часто") == 0


def test_ticker_enqueues_every_interval_with_expiry_equal_to_interval():
    calls = []
    ticker = RecoveryTicker(0.05, calls.append)
    try:
        assert ticker.start() is True
        assert calls == []                                   # первый тик — через промежуток, не сразу
        assert _wait(lambda: len(calls) >= 3)
    finally:
        ticker.stop()
    assert set(calls) == {0.05}                              # задача ждёт в очереди не дольше промежутка — копии не копятся
    assert not ticker.running


def test_ticker_survives_enqueue_errors():
    calls = []

    def flaky(expires):
        calls.append(expires)
        if len(calls) == 1:
            raise ConnectionError("redis недоступен")

    ticker = RecoveryTicker(0.03, flaky)
    try:
        ticker.start()
        assert _wait(lambda: len(calls) >= 3)                # после ошибки расписание продолжается
    finally:
        ticker.stop()


def test_ticker_starts_once_and_zero_interval_is_off():
    ticker = RecoveryTicker(60, lambda e: None)
    try:
        assert ticker.start() is True
        assert ticker.start() is False                       # второй поток не создаётся
        assert sum(t.name == "recover-index-ticker" for t in threading.enumerate()) >= 1
    finally:
        ticker.stop()
    assert RecoveryTicker(0, lambda e: None).start() is False


def test_worker_startup_enqueues_once_then_starts_one_schedule(monkeypatch):
    from app import worker

    sent = []
    monkeypatch.setattr(worker.recover_index_task, "delay", lambda: sent.append(("delay", None)))
    monkeypatch.setattr(worker.recover_index_task, "apply_async", lambda expires=None: sent.append(("async", expires)))
    monkeypatch.setattr(worker.settings, "recover_index_interval_minutes", 45)
    monkeypatch.setattr(worker, "_recovery_ticker", None)
    try:
        worker._schedule_recovery()
        ticker = worker._recovery_ticker
        assert sent == [("delay", None)]                      # при запуске — сразу одна задача
        assert ticker is not None and ticker.running and ticker.interval == 45 * 60
        worker._schedule_recovery()                           # повторный сигнал запуска: ещё задача, но не второй поток
        assert worker._recovery_ticker is ticker and len(sent) == 2
        ticker.tick()
        assert sent[-1] == ("async", 45 * 60)                 # по расписанию — с expires
    finally:
        if worker._recovery_ticker is not None:
            worker._recovery_ticker.stop()


def test_worker_schedule_can_be_turned_off(monkeypatch):
    from app import worker

    monkeypatch.setattr(worker.recover_index_task, "delay", lambda: None)
    monkeypatch.setattr(worker.settings, "recover_index_interval_minutes", 0)
    monkeypatch.setattr(worker, "_recovery_ticker", None)
    worker._schedule_recovery()
    assert worker._recovery_ticker is not None and not worker._recovery_ticker.running


def test_worker_startup_survives_broken_queue(monkeypatch):
    from app import worker

    def broken():
        raise ConnectionError("redis недоступен")

    monkeypatch.setattr(worker.recover_index_task, "delay", broken)
    monkeypatch.setattr(worker.settings, "recover_index_interval_minutes", 0)
    monkeypatch.setattr(worker, "_recovery_ticker", None)
    worker._schedule_recovery()                               # не падает: обработчик сигнала Celery исключения проглотил бы


LITE_SCHEDULE_PROBE = r"""
import json, time
from app import worker
from app.pipeline.ml_runtime import MLNotReadyError
runs = []
def not_ready(*names):
    runs.append(time.monotonic()); raise MLNotReadyError("модель ещё грузится")
worker._require_ml = not_ready
started = worker._start_recovery_schedule()
again = worker._start_recovery_schedule()
end = time.monotonic() + 10
while len(runs) < 2 and time.monotonic() < end:
    time.sleep(0.02)
worker._recovery_ticker.stop()
print(json.dumps({"started": started, "again": again, "runs": len(runs),
                  "interval": worker._recovery_ticker.interval}))
"""


def test_lite_mode_runs_scheduled_recovery_in_process(tmp_path):
    env = {**os.environ, "LITE_MODE": "1", "QUEUE_BROKER_URL": "memory://", "FRAMES_REDIS_URL": "",
           "DATABASE_URL": "sqlite:///" + (tmp_path / "t.db").as_posix(), "PYTHONIOENCODING": "utf-8",
           "RECOVER_INDEX_INTERVAL_MINUTES": "0.002"}
    out = subprocess.run([sys.executable, "-c", LITE_SCHEDULE_PROBE], cwd=ROOT, env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["started"] is True and data["again"] is False
    assert data["runs"] >= 2                                  # задача выполнялась в процессе, по расписанию, не один раз
    assert abs(data["interval"] - 0.12) < 1e-9
