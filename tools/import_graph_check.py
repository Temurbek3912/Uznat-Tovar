#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ищет «висящие» импорты: строки вида `from app.x import y`, где модуля app/x.py больше нет
(например, после удаления файлов). Работает только на стандартной библиотеке (ast),
поэтому запускается где угодно, даже без FastAPI/SQLAlchemy/torch.

Запуск из корня проекта:   python tools/import_graph_check.py
Код выхода: 0 — всё в порядке, 1 — найдены висящие импорты.

Чего НЕ ловит: динамические импорты (importlib, строки), имена внутри пакетов, которые
определены не в __init__.py, и всё, что связано с содержимым файлов (тексты, пути в документации).
Поэтому он дополняет `pytest`, а не заменяет его.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent if Path(__file__).parent.name == "tools" else Path.cwd()
ROOTS = ("app", "scripts", "ml", "tests")


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def top_level_names(init: Path) -> set[str]:
    """Имена, определённые в __init__.py пакета (чтобы отличить подмодуль от атрибута)."""
    names: set[str] = set()
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"))
    except Exception:
        return names
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def main() -> int:
    files = [p for r in ROOTS for p in (ROOT / r).rglob("*.py")] if ROOT.exists() else []
    modules = {module_name(p): p for p in files}
    packages = {module_name(p): p for p in files if p.name == "__init__.py"}
    # пакеты без __init__.py тоже считаем существующими, если в них есть файлы
    for m in list(modules):
        parts = m.split(".")
        for i in range(1, len(parts)):
            packages.setdefault(".".join(parts[:i]), None)
    dangling: dict[str, list[str]] = {}
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            dangling.setdefault(str(p.relative_to(ROOT)), []).append(f"синтаксическая ошибка: {e}")
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module and n.level == 0 and n.module.split(".")[0] in ROOTS:
                base = n.module
                if base not in modules and base not in packages:
                    dangling.setdefault(str(p.relative_to(ROOT)), []).append(f"from {base} import …")
                    continue
                if base in packages:  # from пакет import имя  -> имя это подмодуль или атрибут
                    init = packages[base]
                    defined = top_level_names(init) if init else set()
                    for a in n.names:
                        if a.name != "*" and f"{base}.{a.name}" not in modules and a.name not in defined \
                                and f"{base}.{a.name}" not in packages:
                            dangling.setdefault(str(p.relative_to(ROOT)), []).append(f"from {base} import {a.name}")
            elif isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.split(".")[0] in ROOTS and a.name not in modules and a.name not in packages:
                        dangling.setdefault(str(p.relative_to(ROOT)), []).append(f"import {a.name}")
    if not dangling:
        print(f"OK: висящих импортов нет (проверено файлов: {len(files)})")
        return 0
    print(f"НАЙДЕНЫ висящие импорты в {len(dangling)} файлах:")
    for f, items in sorted(dangling.items()):
        for i in sorted(set(items)):
            print(f"  {f}: {i}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
