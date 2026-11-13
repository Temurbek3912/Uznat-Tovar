"""Запуск обычных Python-тестов БЕЗ pytest — для среды, где pytest поставить нельзя (нет сети: песочница чата). Берёт модули по именам и вызывает
их функции test_*: без параметров или с единственным параметром tmp_path (ему подставляется временная папка, как это делает pytest).
Тесты с другими фикстурами (monkeypatch, client, …) пропускаются и перечисляются — «пропущен» не значит «прошёл». Модуль, который не удалось
импортировать (нужен fastapi/sqlalchemy), печатается как «НЕ ЗАПУЩЕН». Настоящий прогон проекта — `pytest` в Docker; это лишь запасной путь.

    python tools/run_plain_tests.py tests.test_frontend_pages tests.test_check_imports tests.test_pwa_assets
"""
from __future__ import annotations

import importlib
import inspect
import pathlib
import sys
import tempfile
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main(names: list[str]) -> int:
    total = ok = skipped = not_run = 0
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — нужно показать любую причину, почему модуль не запустился
            print(f"НЕ ЗАПУЩЕН {name}: {type(exc).__name__}: {exc}")
            not_run += 1
            continue
        for fn_name, fn in inspect.getmembers(module, inspect.isfunction):
            if not fn_name.startswith("test_") or fn.__module__ != module.__name__:
                continue
            params = list(inspect.signature(fn).parameters)
            if params and params != ["tmp_path"]:
                print(f"пропущен (нужны фикстуры {params}) {name}.{fn_name}")
                skipped += 1
                continue
            total += 1
            try:
                if params:
                    with tempfile.TemporaryDirectory() as folder:
                        fn(pathlib.Path(folder))
                else:
                    fn()
                ok += 1
                print(f"ok   {fn_name}")
            except Exception:  # noqa: BLE001
                print(f"FAIL {fn_name}")
                traceback.print_exc(limit=3)
    print(f"\nИтого: {ok} из {total} прошли; пропущено из-за фикстур: {skipped}; модулей не запущено: {not_run}")
    return 0 if ok == total and not not_run else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
