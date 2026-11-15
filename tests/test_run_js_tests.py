"""tools/run_js_tests.py — запускатель JS-проверок: решает, считать ли набор пройденным, поэтому сам должен быть строгим."""
from __future__ import annotations

from types import SimpleNamespace

from tools import run_js_tests as runner


def test_success_needs_zero_exit_code_and_a_passed_total():
    assert runner.parse_result(0, "ok   a\n\n41 passed\n") == (True, 41)
    assert runner.parse_result(0, "41 passed") == (True, 41)


def test_script_that_stops_midway_without_a_total_is_a_failure_even_with_exit_code_zero():
    assert runner.parse_result(0, "ok   a\nok   b\n") == (False, 0)
    assert runner.parse_result(0, "") == (False, 0)


def test_failures_are_detected_in_every_form_the_suites_print():
    assert runner.parse_result(1, "ok   a\n\n5 passed\n") == (False, 5)                       # код выхода
    assert runner.parse_result(0, "FAIL: AssertionError\n") == (False, 0)                      # аварийная печать
    assert runner.parse_result(0, "ok a\nFAIL: x\n\n3 passed\n") == (False, 3)
    assert runner.parse_result(0, "\n5 passed, 2 FAILED\n") == (False, 5)                      # итог с упавшими
    assert runner.parse_result(0, "ok   обработка не удалась (failed): просим переснять\n\n9 passed\n") == (True, 9)   # слово в названии теста


def test_missing_node_is_reported_not_crashed():
    lines = []
    assert runner.main(which=lambda name: None, out=lines.append) == 2
    assert "Node.js не найден" in lines[0]


def _run_factory(results):
    def run(cmd, **kwargs):
        name = cmd[1].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        code, stdout = results[name]
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")
    return run


def test_main_runs_every_suite_and_sums_checks():
    suites = sorted(p.name for p in (runner.ROOT / "tools").glob("*_smoke_test.js"))
    assert {"admin_smoke_test.js", "login_smoke_test.js", "shell_smoke_test.js"} <= set(suites)
    assert not {"capture_smoke_test.js", "dashboard_smoke_test.js", "review_smoke_test.js", "setup_smoke_test.js"} & set(suites)
    lines = []
    code = runner.main(run=_run_factory({name: (0, "2 passed\n") for name in suites}), which=lambda n: "node", out=lines.append)
    assert code == 0
    assert f"Итого: {2 * len(suites)} проверок в {len(suites)} наборах; все прошли" in lines[-1]


def test_main_fails_and_names_the_failed_suite():
    suites = sorted(p.name for p in (runner.ROOT / "tools").glob("*_smoke_test.js"))
    results = {name: (0, "2 passed\n") for name in suites}
    results["shell_smoke_test.js"] = (1, "ok a\nFAIL: boom\n")
    lines = []
    assert runner.main(run=_run_factory(results), which=lambda n: "node", out=lines.append) == 1
    assert "НЕ ПРОШЛИ: shell_smoke_test.js" in lines[-1]
    assert any(line.startswith("FAIL shell_smoke_test.js") for line in lines)
