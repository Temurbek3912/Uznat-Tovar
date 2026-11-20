"""tools/service_tests.py: адреса временных служб и список модулей (сам Docker здесь не запускается)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("service_tests", ROOT / "tools" / "service_tests.py")
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)


def test_env_points_only_to_local_temporary_services():
    env = st.build_env(55001, 55002, base={"PATH": "x"})
    assert env["FULL_BACKUP_TEST_DB_URL"] == "postgresql://postgres:test@127.0.0.1:55001/postgres"
    assert env["REDIS_TEST_URL"] == "redis://127.0.0.1:55002/0" and env["PATH"] == "x"


def test_default_modules_exist_and_use_the_services():
    assert st.pytest_args(False, []) == st.DEFAULT_TESTS and st.pytest_args(True, []) == []
    assert st.pytest_args(False, ["tests/test_migrations.py", "-x"]) == ["tests/test_migrations.py", "-x"]
    for rel in st.DEFAULT_TESTS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "FULL_BACKUP_TEST_DB_URL" in text or "REDIS_TEST_URL" in text, rel


def test_wait_ready_gives_up_after_timeout():
    calls = []

    class R:
        returncode = 1

    assert st.wait_ready(["x"], timeout=0.01, run=lambda *a, **k: calls.append(a) or R(), sleep=lambda s: None) is False
    assert calls


def test_free_port_is_a_real_port():
    assert 1024 < st.free_port() < 65536
