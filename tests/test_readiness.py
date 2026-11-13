"""app/core/readiness.py — логика /health/ready (выполняется офлайн)."""
from __future__ import annotations

import logging

from app.core.readiness import CheckFailed, run_checks


def _ok():
    return None


def test_all_checks_pass():
    assert run_checks({"database": _ok, "queue": _ok}) == {"status": "ok", "checks": {"database": "ok", "queue": "ok"}}


def test_one_failure_makes_service_unavailable_but_all_checks_still_run():
    ran = []

    def failing():
        ran.append("failing")
        raise ConnectionError("boom")

    def later():
        ran.append("later")

    result = run_checks({"database": failing, "queue": later})
    assert result["status"] == "unavailable" and ran == ["failing", "later"]
    assert result["checks"] == {"database": "fail", "queue": "ok"}


def test_neither_exception_text_nor_class_is_exposed_but_both_reach_the_log(caplog):
    def leaky():
        raise RuntimeError("postgresql://postgres:s3cret@db/x refused")

    with caplog.at_level(logging.WARNING):
        result = run_checks({"database": leaky})
    assert result["checks"] == {"database": "fail"} and "s3cret" not in str(result) and "RuntimeError" not in str(result)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "s3cret" not in logged          # в журнале — класс и текст без пароля


def test_check_failed_message_is_shown_because_it_is_written_by_us():
    def schema():
        raise CheckFailed("нет таблиц: users, stores")

    assert run_checks({"schema": schema})["checks"]["schema"] == "fail: нет таблиц: users, stores"


def test_empty_checks_are_healthy():
    assert run_checks({}) == {"status": "ok", "checks": {}}


def test_password_from_exception_text_does_not_reach_the_server_log_either():
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    logger = logging.getLogger("app.core.readiness")
    logger.addHandler(handler)
    try:
        def leaky():
            raise RuntimeError("could not connect: postgresql://postgres:s3cret@db/x refused")

        run_checks({"database": leaky})
    finally:
        logger.removeHandler(handler)
    assert records and "RuntimeError" in records[0]
    assert not any("s3cret" in r for r in records)
