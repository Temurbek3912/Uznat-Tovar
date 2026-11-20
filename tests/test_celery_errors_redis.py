"""Передача ошибок задачи из воркера в API через НАСТОЯЩИЙ Celery и Redis (§7 Б п. 8 ENGINEERING_STATUS).

Воркер Celery запускается в потоке этого же процесса (celery.contrib.testing), очередь и результаты — в настоящем Redis из
REDIS_TEST_URL (например, временный контейнер redis:7-alpine); без переменной — пропуск. Настройки сериализации — те же, что у
приложения (app/worker.py: JSON). Проверяется путь, которым идёт ошибка добавления товара: задача бросает AppError с кодом и
параметрами → Celery пишет в Redis тип + args → API читает AsyncResult и показывает describe_task (как
GET /products/tasks/{id}). Код, параметры и русский текст не должны теряться; текст чужого исключения не должен попасть клиенту.
"""
from __future__ import annotations

import logging
import os
import uuid

import pytest

celery = pytest.importorskip("celery")
redis = pytest.importorskip("redis")

from celery import Celery  # noqa: E402
from celery.contrib.testing.worker import start_worker  # noqa: E402

from app.core.app_errors import AppError, code_of, params_of  # noqa: E402
from app.core.errors import from_app_error  # noqa: E402
from app.services.catalog_upload import (  # noqa: E402
    GENERIC_FAILURE,
    InputError,
    NotReadyError,
    PhotoError,
    describe_task,
)

URL = os.environ.get("REDIS_TEST_URL")


class _UnknownTypeError(AppError):
    """Класс, которого «нет» на стороне API: имя модуля в записи Celery подменяется на несуществующий."""

    kind = "conflict"


def _make_app() -> Celery:
    from app.worker import celery_app as real

    app = Celery("uznat_err_test_" + uuid.uuid4().hex[:6], broker=URL, backend=URL)
    # Сериализация — как у приложения: задача и результат в JSON, ошибка — тип + args.
    for key in ("task_serializer", "result_serializer", "accept_content", "result_accept_content"):
        app.conf[key] = real.conf[key]
    app.conf.update(task_acks_late=True, worker_prefetch_multiplier=1, task_default_queue="uznat-err-test-" + uuid.uuid4().hex[:6],
                    broker_connection_retry_on_startup=False)

    @app.task(name="uznat_err_test.raise_photo")
    def raise_photo():
        raise PhotoError("Фото слишком маленькое: 10×20", code="photo_too_small", params={"width": 10, "height": 20})

    @app.task(name="uznat_err_test.raise_input")
    def raise_input(name):
        raise InputError(f"Товар «{name}» уже есть", "product_duplicate", {"name": name})

    @app.task(name="uznat_err_test.raise_not_ready")
    def raise_not_ready():
        raise NotReadyError("Модель ещё загружается")

    @app.task(name="uznat_err_test.raise_foreign")
    def raise_foreign():
        raise RuntimeError("psycopg: password authentication failed for user postgres at D:\\secret\\db.conf")

    @app.task(name="uznat_err_test.raise_unknown")
    def raise_unknown():
        err = _UnknownTypeError("Код уже занят", "barcode_taken", {"code": "4600000000001"})
        err.__class__.__module__ = "app.no_such_module_on_api_side"
        raise err

    @app.task(name="uznat_err_test.ok")
    def ok():
        return {"product_id": "p-1", "barcodes_not_linked": [{"code": "123", "name": "Чай «Ахмад»"}]}

    return app


@pytest.fixture(scope="module")
def app():
    if not URL:
        pytest.skip("нет REDIS_TEST_URL — настоящего Redis")
    client = redis.Redis.from_url(URL, socket_connect_timeout=1)
    client.ping()
    app = _make_app()
    app.loader.import_module("celery.contrib.testing.tasks")      # задача ping для start_worker
    # start_worker настраивает журнал процесса (корень — уровень ERROR и свой обработчик) и не возвращает его: без восстановления
    # тесты после этого модуля не видят предупреждений (tests/test_readiness.py падал на Linux и на Windows).
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    try:
        with start_worker(app, pool="solo", perform_ping_check=True, shutdown_timeout=10):
            yield app
    finally:
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]


def _run(app, name, *args):
    res = app.send_task(name, args=args)
    res.get(timeout=30, propagate=False)
    # Как в маршруте: новый AsyncResult по id, прочитанный из backend, а не объект из памяти.
    fresh = app.AsyncResult(res.id)
    assert fresh.ready()
    return fresh


def test_app_error_keeps_class_code_params_and_text(app):
    r = _run(app, "uznat_err_test.raise_photo")
    assert r.status == "FAILURE"
    exc = r.result
    assert type(exc) is PhotoError
    assert (str(exc), exc.code, exc.params) == ("Фото слишком маленькое: 10×20", "photo_too_small", {"width": 10, "height": 20})
    d = describe_task(r.status, r.result)
    assert (d["state"], d["code"], d["params"], d["message"]) == ("failed", "photo_too_small", {"width": 10, "height": 20},
                                                                   "Фото слишком маленькое: 10×20")
    api = from_app_error(exc, fallback_code="catalog_error")
    assert (api.status_code, api.code, api.detail) == (422, "photo_too_small", "Фото слишком маленькое: 10×20")


def test_positional_args_and_task_arguments_with_cyrillic(app):
    r = _run(app, "uznat_err_test.raise_input", "Чай «Ахмад» 100 г")
    d = describe_task(r.status, r.result)
    assert d["code"] == "product_duplicate" and d["params"] == {"name": "Чай «Ахмад» 100 г"}
    assert d["message"] == "Товар «Чай «Ахмад» 100 г» уже есть"


def test_default_code_survives(app):
    r = _run(app, "uznat_err_test.raise_not_ready")
    assert type(r.result) is NotReadyError
    assert r.result.code == NotReadyError.default_code
    d = describe_task(r.status, r.result)
    assert d["code"] == (NotReadyError.default_code or "catalog_error") and d["message"] == "Модель ещё загружается"


def test_foreign_exception_text_does_not_reach_client(app):
    r = _run(app, "uznat_err_test.raise_foreign")
    assert r.status == "FAILURE"
    d = describe_task(r.status, r.result)
    assert (d["code"], d["message"], d["params"]) == ("task_failed", GENERIC_FAILURE, {})
    assert "password" not in repr(d) and "secret" not in repr(d)


def test_class_unknown_on_api_side_still_gives_code_and_params(app):
    """Модуль класса на стороне API не импортируется. Celery (проверено на 5.6) записывает ближайший импортируемый базовый
    класс — AppError; код, параметры и текст читаются из args, describe_task считает ошибку нашей."""
    r = _run(app, "uznat_err_test.raise_unknown")
    exc = r.result
    assert isinstance(exc, Exception)
    assert (str(exc), code_of(exc), params_of(exc)) == ("Код уже занят", "barcode_taken", {"code": "4600000000001"})
    d = describe_task(r.status, r.result)
    assert (d["code"], d["message"]) == ("barcode_taken", "Код уже занят")


def test_success_result_with_notes(app):
    r = _run(app, "uznat_err_test.ok")
    d = describe_task(r.status, r.result)
    assert d["state"] == "done" and d["product_id"] == "p-1"
    assert d["notes"][0]["code"] == "barcode_not_linked" and d["notes"][0]["params"] == {"code": "123", "name": "Чай «Ахмад»"}


# ------------------------------------------------- обработчики запуска app/worker.py не срабатывают для чужого воркера (без Redis)

def _boom(*a, **kw):
    raise AssertionError("обработчик программы сработал для чужого воркера")


def test_worker_signal_handlers_ignore_a_foreign_celery_worker(monkeypatch):
    from types import SimpleNamespace

    from app import worker

    calls = []
    monkeypatch.setattr(worker, "_foreign_worker", False)
    monkeypatch.setattr(worker.ml_runtime, "preflight", _boom)
    monkeypatch.setattr(worker.ml_runtime, "clear_status", _boom)
    monkeypatch.setattr(worker.ml_runtime, "build_runtime", _boom)
    monkeypatch.setattr(worker, "_enqueue_recovery", lambda *a, **kw: calls.append("enqueue"))
    monkeypatch.setattr(worker, "_start_recovery_schedule", lambda: calls.append("schedule"))

    foreign = SimpleNamespace(app=object())                       # WorkController / Consumer другого приложения
    worker._preflight_worker(sender=foreign, signal=object())
    assert worker._foreign_worker is True
    worker._init_ml_components(sender="celery@host", signal=object())   # отправитель — имя узла; решает флаг worker_init
    worker._sweep_stale_frames(sender=foreign, signal=object())
    worker._schedule_recovery(sender=foreign, signal=object())
    assert calls == []

    ours = SimpleNamespace(app=worker.celery_app)
    worker._schedule_recovery(sender=ours, signal=object())
    worker._schedule_recovery()                                   # прямой вызов (облегчённый режим) — как раньше
    assert calls == ["enqueue", "schedule", "enqueue", "schedule"]


def test_own_worker_still_runs_preflight(monkeypatch):
    from types import SimpleNamespace

    from app import worker

    seen = []
    monkeypatch.setattr(worker, "_foreign_worker", True)
    monkeypatch.setattr(worker.ml_runtime, "clear_status", lambda: seen.append("clear"))
    monkeypatch.setattr(worker.ml_runtime, "preflight", lambda *a: seen.append("preflight") or [])
    worker._preflight_worker(sender=SimpleNamespace(app=worker.celery_app), signal=object())
    assert worker._foreign_worker is False and seen == ["clear", "preflight"]
