"""
Запускает все JS-проверки страниц (tools/*_smoke_test.js) через Node.js и печатает итог.

    python tools/run_js_tests.py

Успехом считается только код выхода 0 И строка «N passed» в выводе: обрыв скрипта посреди проверок без итоговой строки — это провал,
а не «ничего не упало». Нужен Node.js (https://nodejs.org); в Docker-образе проекта его нет — эти проверки запускают на компьютере
разработчика. Возвращает 0, если все наборы прошли.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_result(returncode: int, output: str) -> tuple[bool, int]:
    """(успех, число пройденных проверок) по коду выхода и тексту вывода."""
    match = re.search(r"^(\d+) passed\b", output, re.M)
    failed = returncode != 0 or match is None or re.search(r"^FAIL[: ]", output, re.M) is not None or "FAILED" in output
    return (not failed), (int(match.group(1)) if match else 0)


def main(argv: list[str] | None = None, *, run=subprocess.run, which=shutil.which, out=print) -> int:
    node = which("node")
    if not node:
        out("Node.js не найден — JS-проверки пропущены. Установите Node.js с https://nodejs.org и запустите снова.")
        return 2
    suites = sorted((ROOT / "tools").glob("*_smoke_test.js"))
    if not suites:
        out("Не найдено ни одного tools/*_smoke_test.js")
        return 2
    total, bad = 0, []
    for suite in suites:
        result = run([node, str(suite)], capture_output=True, text=True, encoding="utf-8", timeout=180, cwd=str(ROOT))
        ok, count = parse_result(result.returncode, (result.stdout or "") + (result.stderr or ""))
        out(f"{'ok  ' if ok else 'FAIL'} {suite.name:28s} {count:4d} проверок")
        total += count
        if not ok:
            bad.append(suite.name)
            out((result.stdout or "")[-1500:] + (result.stderr or "")[-800:])
    out(f"\nИтого: {total} проверок в {len(suites)} наборах" + (f"; НЕ ПРОШЛИ: {', '.join(bad)}" if bad else "; все прошли"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
