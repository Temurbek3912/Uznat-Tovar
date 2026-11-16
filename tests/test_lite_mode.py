"""
Облегчённый режим (документ 11, §10): выбор по железу (app/core/hardware.py), устройство модели (ML_DEVICE), свой индекс
(CATALOG_INDEX_PATH) и задачи в процессе API (app/worker.py при LITE_MODE=1 — в отдельном процессе Python, чтобы не трогать
настройки остальных тестов). Не проверяется здесь: настоящая модель на процессоре (замер — ml/benchmark_runtime.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

from app.core import hardware
from app.core.hardware import Hardware, plan_lite
from app.pipeline.ml_runtime import embedder_device

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------- выбор по железу

def test_weak_laptop_without_gpu_runs_on_cpu_without_the_vision_model():
    plan = plan_lite(Hardware(ram_gb=8, cores=4))
    assert plan.device == "cpu" and plan.vision is False
    assert any("штрихкод" in n for n in plan.notes)


def test_good_gpu_and_enough_ram_keep_the_vision_model():
    plan = plan_lite(Hardware(ram_gb=32, cores=24, gpu_name="RTX 5060", vram_gb=8.0))
    assert plan.device == "auto" and plan.vision is True and plan.notes == []


def test_small_gpu_or_small_ram_turn_the_vision_model_off():
    assert plan_lite(Hardware(ram_gb=32, cores=8, gpu_name="MX450", vram_gb=2.0)).vision is False
    small = plan_lite(Hardware(ram_gb=8, cores=8, gpu_name="RTX 3060", vram_gb=6.0))
    assert small.vision is False and small.device == "auto" and any("ОЗУ" in n for n in small.notes)


def test_owner_can_force_cpu_and_force_the_vision_model_on_or_off():
    gpu = Hardware(ram_gb=32, cores=24, gpu_name="RTX", vram_gb=8.0)
    forced = plan_lite(gpu, force_cpu=True)
    assert forced.device == "cpu" and forced.vision is False
    on = plan_lite(Hardware(ram_gb=8, cores=4), vision="on")
    assert on.vision is True and any("десятки секунд" in n for n in on.notes)
    assert plan_lite(gpu, vision="off").vision is False


def test_very_small_ram_gets_a_warning():
    assert any("меньше минимума" in n for n in plan_lite(Hardware(ram_gb=4, cores=2)).notes)


def test_nvidia_gpu_probe_reads_nvidia_smi_and_survives_its_absence():
    run = lambda argv, **kw: NS(returncode=0, stdout="NVIDIA GeForce RTX 5060 Laptop GPU, 8151\n")   # noqa: E731
    name, vram = hardware.nvidia_gpu(run=run, which=lambda n: "nvidia-smi")
    assert name == "NVIDIA GeForce RTX 5060 Laptop GPU" and round(vram, 1) == 8.0
    assert hardware.nvidia_gpu(run=run, which=lambda n: None) == (None, None)
    broken = lambda argv, **kw: NS(returncode=9, stdout="")                                          # noqa: E731
    assert hardware.nvidia_gpu(run=broken, which=lambda n: "x") == (None, None)


def test_probe_reports_real_ram_and_cores_of_this_computer():
    hw = hardware.probe(which=lambda n: None)
    assert hw.ram_gb > 0 and hw.cores >= 1 and hw.gpu_name is None
    assert "нет видеокарты NVIDIA" in hw.describe()


# ----------------------------------------------------------------------- устройство модели и свой индекс

def test_embedder_device_from_environment():
    assert embedder_device({}) == ("cuda", 0)                      # auto: видеокарта, если есть (DinoV2Embedder сам проверит)
    assert embedder_device({"ML_DEVICE": "cpu", "ML_THREADS": "4"}) == ("cpu", 4)
    assert embedder_device({"ML_DEVICE": "что-то", "ML_THREADS": "x"}) == ("cuda", 0)


def test_catalog_index_path_override(monkeypatch):
    from app.core import config
    monkeypatch.setenv("CATALOG_INDEX_PATH", "data/lite/index/catalog.faiss")
    assert config._matcher_config({"matcher": {"index_path": "models/index/catalog.faiss"}}).index_path == \
        "data/lite/index/catalog.faiss"
    monkeypatch.delenv("CATALOG_INDEX_PATH")
    assert config._matcher_config({"matcher": {"index_path": "models/index/catalog.faiss"}}).index_path == \
        "models/index/catalog.faiss"
    assert config._matcher_config({}) is None


# ----------------------------------------------------------------------- задачи в процессе API

LITE_PROBE = r"""
import json, threading, time
from app.worker import celery_app, _serial, identify_product_task
from app.core.frame_store import get_frame_store, MemoryFrameStore
conf = celery_app.conf
order = []

@celery_app.task(name="test.slow")
def slow(tag):
    order.append(("start", tag)); time.sleep(0.2); order.append(("end", tag)); return tag

threads = [threading.Thread(target=lambda t=t: slow.apply_async(args=[t]).get()) for t in "ab"]
[t.start() for t in threads]; [t.join() for t in threads]
res = slow.apply_async(args=["c"])
again = celery_app.AsyncResult(res.id)
print(json.dumps({"eager": conf.task_always_eager, "broker": conf.broker_url, "store": type(get_frame_store()).__name__,
                  "order": [e for e, _ in order], "status": again.status, "result": again.result}))
"""


def test_lite_mode_runs_tasks_in_process_one_at_a_time_with_results_for_progress_polling(tmp_path):
    env = {**os.environ, "LITE_MODE": "1", "QUEUE_BROKER_URL": "memory://", "FRAMES_REDIS_URL": "",
           "DATABASE_URL": "sqlite:///" + (tmp_path / "t.db").as_posix(), "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", LITE_PROBE], cwd=ROOT, env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["eager"] is True and data["broker"] == "memory://" and data["store"] == "MemoryFrameStore"
    assert data["order"] == ["start", "end", "start", "end", "start", "end"]       # по одной, не параллельно
    assert data["status"] == "SUCCESS" and data["result"] == "c"                   # «ход добавления товара» виден


CONCURRENT_GET_PROBE = r"""
import json, threading
from app.worker import celery_app

started, release = threading.Event(), threading.Event()

@celery_app.task(name="test.busy")
def busy():
    started.set(); release.wait(10); return "busy"

@celery_app.task(name="test.quick")
def quick():
    return "quick"

done = quick.apply_async()                    # результат второго снимка уже готов
worker = threading.Thread(target=lambda: busy.apply_async())
worker.start(); started.wait(10)              # а в другом потоке идёт распознавание первого
out = {}
try:
    done.get(timeout=5, propagate=False)
    out["plain"] = "ok"
except RuntimeError:
    out["plain"] = "raised"
out["fixed"] = done.get(timeout=5, propagate=False, disable_sync_subtasks=False)
release.set(); worker.join()
print(json.dumps(out))
"""


def test_lite_mode_second_photo_gets_its_answer_while_another_photo_is_being_recognised(tmp_path):
    # Замер 2026-11-16: два покупателя одновременно — второй получал «недоступно» (Celery принимал get() из запроса за get() внутри
    # задачи). run_identify ждёт ответ с disable_sync_subtasks=False — так get() работает и пока чужая задача идёт.
    env = {**os.environ, "LITE_MODE": "1", "QUEUE_BROKER_URL": "memory://", "FRAMES_REDIS_URL": "",
           "DATABASE_URL": "sqlite:///" + (tmp_path / "t.db").as_posix(), "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", CONCURRENT_GET_PROBE], cwd=ROOT, env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data == {"plain": "raised", "fixed": "quick"}
    text = (ROOT / "app" / "api" / "routes_catalog.py").read_text(encoding="utf-8")
    assert "propagate=False, disable_sync_subtasks=False)" in text


def test_lite_sqlite_pool_is_large_enough_for_a_crowd(tmp_path):
    # Замер 2026-11-16: при 100 покупателях список товаров падал с 500 — стандартных 5 + 10 соединений SQLite не хватало.
    probe = ("import json; from app.db.session import engine; "
             "print(json.dumps([engine.pool.size(), engine.pool._max_overflow]))")
    env = {**os.environ, "LITE_MODE": "1", "QUEUE_BROKER_URL": "memory://", "FRAMES_REDIS_URL": "",
           "DATABASE_URL": "sqlite:///" + (tmp_path / "t.db").as_posix(), "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    size, overflow = json.loads(out.stdout.strip().splitlines()[-1])
    assert size + overflow >= 100


def test_full_mode_keeps_the_redis_queue():
    from app.worker import celery_app
    assert not celery_app.conf.task_always_eager


def test_readiness_and_admin_panel_do_not_ping_redis_in_lite_mode(monkeypatch):
    from app import main
    from app.api import routes_admin
    monkeypatch.setattr(main.settings, "lite_mode", True)
    main._check_queue()                                                             # не бросает: Redis не нужен
    monkeypatch.setattr(routes_admin, "get_settings", lambda: NS(lite_mode=True))
    assert routes_admin._queue_state() == "ok"
    assert routes_admin._worker_state()["nodes"] == ["в процессе API (облегчённый режим)"]


# ----------------------------------------------------------------------- перенос из полного режима (scripts/lite_copy.py)

def test_copy_rows_moves_every_table_and_lite_has_data_detects_it(tmp_path):
    from sqlalchemy import create_engine, func, select
    from app.db.migrate import upgrade_to_head
    from app.db.models import Product
    from scripts import lite_copy
    src = create_engine("sqlite:///" + (tmp_path / "src.db").as_posix())
    dst = create_engine("sqlite:///" + (tmp_path / "dst.db").as_posix())
    upgrade_to_head(src)
    upgrade_to_head(dst)
    assert lite_copy.lite_has_data(dst) is False
    with src.begin() as conn:
        conn.execute(Product.__table__.insert(), [{"id": "p1", "sku": "S1", "name": "Кола", "brand": "", "category": ""}, {"id": "p2", "sku": "S2", "name": "Чай", "brand": "", "category": ""}])
    counts = lite_copy.copy_rows(src, dst, out=lambda *_: None)
    assert counts["products"] == 2
    with dst.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Product.__table__)).scalar() == 2
    assert lite_copy.lite_has_data(dst) is True


def test_copy_files_skips_lock_files(tmp_path):
    from scripts import lite_copy
    (tmp_path / "a" / "x").mkdir(parents=True)
    (tmp_path / "a" / "catalog.faiss").write_bytes(b"1")
    (tmp_path / "a" / "catalog.faiss.lock").write_bytes(b"")
    (tmp_path / "a" / "x" / "p.jpg").write_bytes(b"2")
    assert lite_copy.copy_files(tmp_path / "a", tmp_path / "b") == 2
    assert not (tmp_path / "b" / "catalog.faiss.lock").exists() and (tmp_path / "b" / "x" / "p.jpg").read_bytes() == b"2"
    assert lite_copy.copy_files(tmp_path / "missing", tmp_path / "c") == 0
