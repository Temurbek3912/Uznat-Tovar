"""scripts/preflight.py — логика проверок окружения на подставных пробах (без реальных Postgres/Redis/GPU)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

from scripts import preflight as pf
from scripts.preflight import (
    FAIL, OK, SKIP, WARN, DbState, Probes, check_cuda, check_db, check_disk, check_packages, check_python,
    check_tcp, check_tesseract, check_weights, format_results, hf_cache_has_model, parse_host_port, run_checks,
)


def by_name(results, name):
    return next(r for r in results if r.name == name)


def statuses(results):
    return {r.name: r.status for r in results}


# ------------------------------------------------------------------ точечные проверки

def test_python_version():
    assert check_python((3, 12, 3)).status == OK
    assert check_python((3, 10, 0)).status == OK
    assert check_python((3, 9, 18)).status == FAIL


def test_packages_missing_required_is_fail_with_pip_hint_and_optional_is_warn():
    ok = lambda name: "1.0"
    res, missing = check_packages(ok)
    assert res.status == OK and missing == set()

    def importer(name):
        if name in ("torch", "psycopg"):
            raise ImportError(name)
        return "1"
    res, missing = check_packages(importer)
    assert res.status == FAIL and missing == {"torch", "psycopg"}
    assert "torch" in res.detail and "psycopg[binary]" in res.hint


def test_shelf_detector_and_spare_db_driver_are_optional():
    """«Узнать товар» работает без детектора полок (ultralytics) и без запасного драйвера psycopg2 — не [FAIL]."""
    def importer(name):
        if name in ("ultralytics", "psycopg2"):
            raise ImportError(name)
        return "1"
    res, missing = check_packages(importer)
    assert res.status == WARN and missing == {"ultralytics", "psycopg2"}

    def importer_opt(name):
        if name == "faiss":
            raise ImportError(name)
        return "1"
    res, missing = check_packages(importer_opt)
    assert res.status == WARN and missing == {"faiss"} and "faiss-cpu" in res.hint


class FakeTorch:
    def __init__(self, available=True, name="NVIDIA GeForce RTX 5060", cap=(12, 0), archs=("sm_90", "sm_120")):
        self.cuda = NS(is_available=lambda: available, get_device_name=lambda i: name,
                       get_device_capability=lambda i: cap, get_arch_list=lambda: list(archs))


def test_cuda_blackwell_requires_a_torch_build_with_sm_120():
    ok = check_cuda(FakeTorch())
    assert ok.status == OK and "sm_120" in ok.detail
    bad = check_cuda(FakeTorch(archs=("sm_80", "sm_86", "sm_90")))               # типичная сборка cu121/cu124
    assert bad.status == FAIL and "cu128" in bad.hint and "5060" in bad.detail
    assert check_cuda(FakeTorch(archs=("sm_80", "compute_120"))).status == OK     # PTX-совместимость тоже годится
    # capability 12.1: собственного sm_121 в сборке нет, но бинарники sm_120 той же линейки Blackwell подходят
    assert check_cuda(FakeTorch(cap=(12, 1), archs=("sm_90", "sm_120"))).status == OK
    assert check_cuda(FakeTorch(cap=(12, 1), archs=("sm_90",))).status == FAIL
    older = check_cuda(FakeTorch(name="RTX 4070", cap=(8, 9), archs=("sm_80", "sm_86", "sm_89")))
    assert older.status == OK                                                      # не-Blackwell не трогаем


def test_cuda_cpu_only_and_missing_torch():
    assert check_cuda(FakeTorch(available=False)).status == WARN
    assert check_cuda(None).status == SKIP


def test_tesseract_missing_binary_missing_languages_and_ok():
    assert check_tesseract(lambda n: None, None).status == WARN
    r = check_tesseract(lambda n: "/usr/bin/tesseract", lambda: ["eng", "rus"])
    assert r.status == WARN and r.detail == "нет языковых пакетов: uzb, uzb_cyrl"     # rus есть — не перечисляется
    assert check_tesseract(lambda n: "/x", lambda: ["eng", "rus", "uzb", "uzb_cyrl"]).status == OK

    def boom():
        raise RuntimeError("tessdata не найден")
    assert check_tesseract(lambda n: "/x", boom).status == WARN


def test_weights_variants():
    base = dict(detector_path="models/detector/y.pt", embedder_path="models/embedder/ft", hub_model="facebook/dinov2-base")
    res = check_weights(detector_exists=True, detector_path=base["detector_path"], embedder_dir_exists=True,
                        embedder_path=base["embedder_path"], hub_model=base["hub_model"], hub_cached=False)
    assert [r.status for r in res] == [OK, OK]
    res = check_weights(False, base["detector_path"], False, base["embedder_path"], base["hub_model"], True)
    assert [r.status for r in res] == [WARN, OK] and "эвристика" in res[0].detail and "уже скачана" in res[1].detail
    res = check_weights(False, base["detector_path"], False, base["embedder_path"], base["hub_model"], False)
    assert res[1].status == WARN and "интернет" in res[1].hint


def test_parse_host_port_and_tcp_check():
    assert parse_host_port("postgresql://u:p@db.local:5433/x", 5432) == ("db.local", 5433)
    assert parse_host_port("redis://localhost/0", 6379) == ("localhost", 6379)
    assert parse_host_port("postgresql://u:p@/x", 5432)[1] == 5432

    def down(host, port, timeout):
        raise ConnectionRefusedError()
    r = check_tcp("Redis", "localhost", 6379, down, "docker compose up -d redis")
    assert r.status == FAIL and "localhost:6379" in r.detail and "docker compose" in r.hint
    assert check_tcp("Redis", "localhost", 6379, lambda h, p, t: None, "").status == OK


def test_db_states():
    down = check_db(DbState(False, "OperationalError: refused"))
    assert [r.status for r in down] == [FAIL] and "refused" in down[0].detail
    no_tables = check_db(DbState(True, tables={"stores"}))
    assert [r.status for r in no_tables] == [OK, WARN] and "users" in no_tables[1].detail
    empty = check_db(DbState(True, tables=set(pf.EXPECTED_TABLES)))
    assert [r.status for r in empty] == [OK, OK, WARN, WARN, WARN]
    full = check_db(DbState(True, tables=set(pf.EXPECTED_TABLES), admin_count=1, store_count=2, product_count=30))
    assert all(r.status == OK for r in full)


def test_media_root_and_disk():
    def deny(path):
        raise PermissionError("read-only")
    assert pf.check_media_root(Path("/x"), deny).status == FAIL
    assert pf.check_media_root(Path("/x"), lambda p: None).status == OK
    assert check_disk(2 * 1024 ** 3, "/x").status == WARN
    assert check_disk(50 * 1024 ** 3, "/x").status == OK


def test_hf_cache_detection(tmp_path):
    env = {"HF_HOME": str(tmp_path)}
    assert hf_cache_has_model("facebook/dinov2-base", env=env) is False
    snaps = tmp_path / "hub" / "models--facebook--dinov2-base" / "snapshots"
    snaps.mkdir(parents=True)
    assert hf_cache_has_model("facebook/dinov2-base", env=env) is False          # папка есть, но пустая — не считается
    (snaps / "abc123").mkdir()
    assert hf_cache_has_model("facebook/dinov2-base", env=env) is True
    assert hf_cache_has_model("facebook/dinov2-base", env={"HF_HUB_CACHE": str(tmp_path / "hub")}) is True


# ------------------------------------------------------------------ сборка

def _settings(**kw):
    base = dict(environment="development", database_url="postgresql://u:p@localhost:5432/db",
                queue_broker_url="redis://localhost:6379/0", media_root="data",
                detector=NS(weights_path="models/detector/y.pt"), embedder=NS(weights_path="models/embedder/ft"))
    base.update(kw)
    return NS(**base)


def _probes(**over):
    base = dict(
        python_version=(3, 12, 3), importer=lambda n: "1", which=lambda n: "/usr/bin/tesseract",
        connector=lambda h, p, t: None, load_settings=lambda: _settings(),
        get_ocr_languages=lambda: ["eng", "rus", "uzb", "uzb_cyrl"],
        path_exists=lambda p: True, dir_exists=lambda p: True, disk_free=lambda p: 100 * 1024 ** 3,
        write_probe=lambda p: None,
        db_state=lambda s: DbState(True, "", set(pf.EXPECTED_TABLES), 1, 1, 10),
        hub_cached=lambda m: True, torch_module=lambda: FakeTorch(), project_root=Path("/proj"),
    )
    base.update(over)
    return Probes(**base)


def test_everything_fine_has_no_fail_and_says_ready(tmp_path):
    # эмбеддер-директория «существует» через roots=[cwd, project_root] -> подставим реальную папку
    (tmp_path / "models" / "embedder" / "ft").mkdir(parents=True)
    res = run_checks(_probes(project_root=tmp_path))
    assert not [r for r in res if r.status == FAIL], format_results(res)
    assert statuses(res)["Веса эмбеддера (поиск по виду)"] == OK
    assert "Можно запускать." in format_results(res)


def test_missing_packages_skip_dependent_checks_without_crashing():
    def importer(name):
        if name in ("torch", "pydantic", "pydantic_settings", "pytesseract"):
            raise ImportError(name)
        return "1"
    res = run_checks(_probes(importer=importer))
    st = statuses(res)
    assert st["Пакеты Python"] == FAIL and st["Настройки"] == SKIP and st["GPU (CUDA)"] == SKIP
    assert st["Веса, Postgres, Redis, таблицы, диск"] == SKIP
    assert "Postgres" not in st and "Сначала исправьте" in format_results(res)


def test_settings_load_failure_is_fail_and_skips_the_rest():
    def broken():
        raise ValueError("bad yaml")
    res = run_checks(_probes(load_settings=broken))
    assert statuses(res)["Настройки"] == FAIL and "bad yaml" in by_name(res, "Настройки").detail
    assert statuses(res)["Веса, Postgres, Redis, таблицы, диск"] == SKIP


def test_postgres_down_skips_db_checks_but_redis_is_checked_independently():
    def connector(host, port, timeout):
        if port == 5432:
            raise ConnectionRefusedError()
    res = run_checks(_probes(connector=connector))
    st = statuses(res)
    assert st["Postgres"] == FAIL and st["Redis"] == OK and st["База данных"] == SKIP
    assert "Таблицы БД" not in st


def test_redis_down_is_fail_even_when_db_is_fine():
    def connector(host, port, timeout):
        if port == 6379:
            raise ConnectionRefusedError()
    res = run_checks(_probes(connector=connector))
    assert statuses(res)["Redis"] == FAIL and statuses(res)["База данных"] == OK


def test_blackwell_wrong_torch_build_is_reported_as_fail_in_full_run():
    res = run_checks(_probes(torch_module=lambda: FakeTorch(archs=("sm_80", "sm_90"))))
    assert statuses(res)["GPU (CUDA)"] == FAIL


def test_unwritable_media_root_is_fail():
    def deny(path):
        raise PermissionError("nope")
    assert statuses(run_checks(_probes(write_probe=deny)))["Папка снимков"] == FAIL


def test_absolute_media_root_is_not_joined_with_project_root():
    seen = {}
    def probe(path):
        seen["path"] = path
    run_checks(_probes(load_settings=lambda: _settings(media_root="/srv/media"), write_probe=probe))
    assert seen["path"] == Path("/srv/media")
    run_checks(_probes(load_settings=lambda: _settings(media_root="data"), write_probe=probe, project_root=Path("/proj")))
    assert seen["path"] == Path("/proj/data")


def test_format_shows_hints_only_for_problems_and_ends_with_summary():
    text = format_results([
        pf.Result(OK, "A", "fine", "ignored hint"), pf.Result(WARN, "B", "meh", "do this"), pf.Result(FAIL, "C", "bad", "fix that"),
        pf.Result(SKIP, "D", "skipped", "never shown"),
    ])
    assert "ignored hint" not in text and "never shown" not in text
    assert "-> do this" in text and "-> fix that" in text
    assert "OK 1, предупреждений 1, блокирующих 1" in text and "Сначала исправьте" in text


def test_main_exit_code_is_nonzero_only_when_there_is_a_blocking_problem():
    assert pf.main(_probes(project_root=Path("/proj"))) == 0                                  # предупреждения не блокируют
    def down(host, port, timeout):
        raise ConnectionRefusedError()
    assert pf.main(_probes(connector=down)) == 1
