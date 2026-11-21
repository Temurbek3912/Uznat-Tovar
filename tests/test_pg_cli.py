"""
tests/pg_cli.py: psql из тестов не может зависнуть навсегда (2026-11-21). Без PostgreSQL — проверка аргументов вызова и того, что
ни один тест не зовёт psql в обход помощника; зависание — настоящим дочерним процессом, который не отвечает.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests import pg_cli


def test_psql_is_called_with_timeouts_and_utf8():
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="ёлка\n", stderr="")

    assert pg_cli.psql("-At", "-c", "select 1", run=run) == "ёлка\n"
    assert seen["cmd"] == ["psql", "-X", "-At", "-c", "select 1"]
    assert seen["timeout"] == pg_cli.PSQL_TIMEOUT_SECONDS and seen["check"] is True
    assert seen["encoding"] == "utf-8" and seen["capture_output"] is True
    assert seen["env"]["PGCONNECT_TIMEOUT"] == str(pg_cli.CONNECT_TIMEOUT_SECONDS)
    assert seen["env"]["PGCLIENTENCODING"] == "UTF8"


def test_env_keeps_the_rest_of_the_environment():
    env = pg_cli.psql_env({"PATH": "x", "PGCONNECT_TIMEOUT": "0"})
    assert env["PATH"] == "x" and env["PGCONNECT_TIMEOUT"] == str(pg_cli.CONNECT_TIMEOUT_SECONDS)   # 0 — «ждать вечно» — заменён


def test_hanging_command_is_stopped_by_the_timeout():
    """Тот же subprocess.run с тем же timeout, что у помощника: зависший процесс завершается, тест получает TimeoutExpired."""
    def run(cmd, **kw):
        return subprocess.run([sys.executable, "-c", "import time; time.sleep(60)"],
                              **{k: v for k, v in kw.items() if k != "env"})

    with pytest.raises(subprocess.TimeoutExpired):
        pg_cli.psql("-c", "select 1", timeout=1, run=run)


def test_no_test_calls_psql_directly():
    here = Path(__file__).parent
    direct = re.compile(r"""subprocess\.(run|call|check_output|Popen)\(\s*\[\s*["']psql["']""")
    offenders = [p.name for p in here.glob("test_*.py") if direct.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
