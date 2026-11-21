"""
psql из тестов — всегда с тайм-аутами (2026-11-21).

Раньше тесты на настоящем PostgreSQL (tests/test_text_search.py, tests/test_full_backup.py) вызывали psql без своего срока:
если сервер не отвечает (не тот адрес, брандмауэр, повисшая блокировка таблицы), весь pytest ждал бы бесконечно.
Теперь:
  - подключение — не дольше CONNECT_TIMEOUT_SECONDS (PGCONNECT_TIMEOUT; параметр connect_timeout в самом адресе важнее);
  - вся команда — не дольше PSQL_TIMEOUT_SECONDS: subprocess.run сам завершает psql и бросает TimeoutExpired, тест падает
    с понятной причиной, а не висит;
  - вывод читается как UTF-8 (PGCLIENTENCODING=UTF8): на Windows кодировка консоли иначе портит кириллицу в ответах;
  - -X: личный ~/.psqlrc не меняет вывод.
"""
from __future__ import annotations

import os
import subprocess

PSQL_TIMEOUT_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 10


def psql_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["PGCONNECT_TIMEOUT"] = str(CONNECT_TIMEOUT_SECONDS)
    env["PGCLIENTENCODING"] = "UTF8"
    return env


def psql(*args: str, timeout: float = PSQL_TIMEOUT_SECONDS, run=subprocess.run) -> str:
    """psql -X <args>; при ошибке SQL — CalledProcessError (нужен -v ON_ERROR_STOP=1 в args), при зависании — TimeoutExpired."""
    return run(["psql", "-X", *args], capture_output=True, text=True, encoding="utf-8", check=True, timeout=timeout,
               env=psql_env()).stdout
