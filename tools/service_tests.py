"""
Тесты, которым нужны настоящие PostgreSQL и Redis: поднимает ВРЕМЕННЫЕ контейнеры postgres:16-alpine и redis:7-alpine,
запускает pytest с адресами FULL_BACKUP_TEST_DB_URL и REDIS_TEST_URL и удаляет контейнеры (данные — только в памяти контейнера).

    python tools/service_tests.py                   # модули, которые без этих служб пропускаются (по умолчанию)
    python tools/service_tests.py --all             # весь pytest
    python tools/service_tests.py tests/test_migrations.py -x     # свои аргументы pytest

Рабочие контейнеры программы (retail-shelf-audit-db-1 и т. п.) и чужие контейнеры не трогаются: у временных свои имена
(uznat-test-pg-…, uznat-test-redis-…), порты выбираются свободные и слушаются только на 127.0.0.1. Образы должны быть уже
скачаны (они есть после установки программы в Docker); иначе Docker скачает их сам (≈ 0,5 ГБ).

Тесты полной копии (tests/test_full_backup.py, 6 шт.) и поиска на PostgreSQL (tests/test_text_search.py, 1) требуют ещё утилит
pg_dump / psql на этом компьютере той же основной версии, что и сервер (16; на Linux — пакет postgresql-client-16 из
apt.postgresql.org: у Debian 13 только 17, а копия утилитами 17 не восстанавливается на сервер 16) — без них они пропускаются.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TESTS = [
    "tests/test_catalog_photos.py",
    "tests/test_migrations.py",
    "tests/test_product_images.py",
    "tests/test_full_backup.py",
    "tests/test_text_search.py",
    "tests/test_throttle_redis.py",
    "tests/test_celery_errors_redis.py",
]
PG_IMAGE, REDIS_IMAGE = "postgres:16-alpine", "redis:7-alpine"
PG_PASSWORD = "test"          # временный сервер на 127.0.0.1, удаляется после прогона


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def docker(*args: str, check: bool = True, run=subprocess.run) -> subprocess.CompletedProcess:
    return run(["docker", *args], capture_output=True, text=True, check=check, encoding="utf-8", errors="replace")


def wait_ready(cmd: list[str], *, timeout: float = 60.0, run=subprocess.run, sleep=time.sleep) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run(["docker", *cmd], capture_output=True).returncode == 0:
            return True
        sleep(0.5)
    return False


def build_env(pg_port: int, redis_port: int, base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    env["FULL_BACKUP_TEST_DB_URL"] = f"postgresql://postgres:{PG_PASSWORD}@127.0.0.1:{pg_port}/postgres"
    env["REDIS_TEST_URL"] = f"redis://127.0.0.1:{redis_port}/0"
    return env


def pytest_args(ns_all: bool, extra: list[str]) -> list[str]:
    if extra:
        return extra
    return [] if ns_all else list(DEFAULT_TESTS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="весь pytest, а не только модули со службами")
    ns, extra = parser.parse_known_args(argv)

    try:
        docker("version", "--format", "{{.Server.Version}}")
    except (OSError, subprocess.CalledProcessError):
        print("Docker не запущен или не установлен — временные PostgreSQL и Redis поднять нельзя.")
        return 2

    tag = uuid.uuid4().hex[:6]
    pg_name, redis_name = f"uznat-test-pg-{tag}", f"uznat-test-redis-{tag}"
    pg_port, redis_port = free_port(), free_port()
    started: list[str] = []
    try:
        docker("run", "-d", "--rm", "--name", pg_name, "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
               "-p", f"127.0.0.1:{pg_port}:5432", PG_IMAGE)
        started.append(pg_name)
        docker("run", "-d", "--rm", "--name", redis_name, "-p", f"127.0.0.1:{redis_port}:6379", REDIS_IMAGE)
        started.append(redis_name)
        # pg_isready внутри контейнера отвечает ещё до перезапуска сервера после initdb — ждём настоящий запрос по TCP.
        if not wait_ready(["exec", pg_name, "psql", "-h", "127.0.0.1", "-U", "postgres", "-Atc", "select 1"]):
            print("Временный PostgreSQL не поднялся за минуту.")
            return 2
        if not wait_ready(["exec", redis_name, "redis-cli", "ping"]):
            print("Временный Redis не поднялся за минуту.")
            return 2
        cmd = [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:warnings", *pytest_args(ns.all, extra)]
        print("PostgreSQL:", pg_port, " Redis:", redis_port, " ->", " ".join(cmd[1:]), flush=True)
        return subprocess.run(cmd, cwd=str(ROOT), env=build_env(pg_port, redis_port)).returncode
    finally:
        for name in started:
            docker("stop", "-t", "2", name, check=False)       # --rm: остановленный контейнер удаляется сам


if __name__ == "__main__":
    sys.exit(main())
