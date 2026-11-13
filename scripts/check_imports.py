"""
Аудит зависимостей: каждый сторонний импорт в коде проекта должен быть (1) объявлен в requirements.txt и
(2) реально установлен в образе. Так «контейнер запустился, а приложение упало из-за отсутствующего пакета»
превращается в ошибку СБОРКИ образа, а не в сюрприз при первом запросе.

    python -m scripts.check_imports              # статически: импорты кода против requirements.txt (без установки пакетов)
    python -m scripts.check_imports --runtime    # то же + пакеты реально находятся в окружении + системный tesseract

Dockerfile вызывает `--runtime` после установки зависимостей; tests/test_check_imports.py вызывает статическую
часть на самом репозитории, поэтому новая зависимость, забытая в requirements.txt, ловится тестом.

Только stdlib: скрипт должен работать там, где зависимости ещё не установлены.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("app", "scripts", "ml", "tools", "tests")

# Инструменты разработчика, которые ставятся отдельно и в образ приложения НЕ входят: {папка: {модули}}. Исключается ТОЛЬКО указанный модуль
# и ТОЛЬКО в указанной папке: тот же import в app/ (или в другой папке) по-прежнему обязан быть в requirements.txt, а остальные
# импорты этих же файлов проверяются как обычно. Без этого `--runtime` при сборке образа (Dockerfile) падал бы на снимках экранов.
# Playwright: tools/ui_screenshots/*.py (pip install playwright && playwright install chromium — только на компьютере разработчика).
DEV_ONLY_IMPORTS = {"tools/ui_screenshots": {"playwright"}}

# Имя для import -> имя пакета в pip (если они различаются). Остальные: имя совпадает после нормализации.
IMPORT_TO_DIST = {
    "cv2": "opencv-python-headless",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "multipart": "python-multipart",
    "faiss": "faiss-cpu",
    "sklearn": "scikit-learn",
    # Starlette — обязательная зависимость FastAPI (ставится вместе с ним); отдельной строки в requirements.txt не нужно.
    # Импортируется напрямую тестом tests/test_body_limit.py (настоящий разбор multipart без FastAPI).
    "starlette": "fastapi",
}

# Драйверы Postgres код не импортирует (SQLAlchemy грузит их по схеме URL — см. app/db/url.py), поэтому статический
# анализ их не видит. Проверяем отдельно: без них приложение падает только при первом подключении к БД.
RUNTIME_ONLY_MODULES = {"psycopg": "psycopg[binary]", "psycopg2": "psycopg2-binary"}

# Что обязано быть из системных пакетов (apt) — pip их не ставит.
OCR_LANGS_WANTED = ("rus", "uzb", "uzb_cyrl", "eng")


def normalize_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def parse_requirements(text: str) -> set[str]:
    """Имена пакетов из requirements.txt: без версий, extras, комментариев и директив (-r/-c/--index-url)."""
    result: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[\s<>=!~;\[@]", line, maxsplit=1)[0]
        if name:
            result.add(normalize_dist(name))
    return result


def local_names(root: Path) -> set[str]:
    """Свои пакеты/модули (app, scripts, ml, …) — не сторонние зависимости."""
    names = {d for d in SCAN_DIRS}
    for child in root.iterdir():
        if child.is_dir() and not child.name.startswith("."):
            names.add(child.name)
        elif child.suffix == ".py":
            names.add(child.stem)
    return names


def scan_imports(root: Path, dirs: tuple[str, ...] = SCAN_DIRS) -> dict[str, set[str]]:
    """{верхнеуровневый модуль: {файлы, где он импортируется}} — только сторонние (не stdlib, не свои)."""
    stdlib = set(sys.stdlib_module_names)
    own = local_names(root)
    found: dict[str, set[str]] = {}
    for d in dirs:
        base = root / d
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue                      # битый файл — не дело этого скрипта (его поймают тесты/статчекер)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                rel = path.relative_to(root).as_posix()
                for name in names:
                    if name in stdlib or name in own or name == "__future__":
                        continue
                    if any(rel.startswith(folder + "/") and name in modules for folder, modules in DEV_ONLY_IMPORTS.items()):
                        continue                  # инструмент разработчика, ставится отдельно (см. DEV_ONLY_IMPORTS)
                    found.setdefault(name, set()).add(rel)
    return found


def dist_for(module: str) -> str:
    return normalize_dist(IMPORT_TO_DIST.get(module, module))


def audit_static(root: Path = ROOT) -> list[str]:
    """Проблемы: импорты, чьего пакета нет в requirements.txt, и драйверы Postgres, которых там нет."""
    req_file = root / "requirements.txt"
    if not req_file.is_file():
        return ["requirements.txt не найден"]
    declared = parse_requirements(req_file.read_text(encoding="utf-8"))

    problems = []
    for module, files in sorted(scan_imports(root).items()):
        if dist_for(module) not in declared:
            where = ", ".join(sorted(files)[:3]) + (" …" if len(files) > 3 else "")
            problems.append(f"import {module} ({where}): пакет «{dist_for(module)}» не объявлен в requirements.txt")
    for module, pip_name in RUNTIME_ONLY_MODULES.items():
        if normalize_dist(re.split(r"[\[<>=]", pip_name, maxsplit=1)[0]) not in declared:
            problems.append(f"драйвер Postgres {module}: «{pip_name}» не объявлен в requirements.txt")
    return problems


def audit_runtime(root: Path = ROOT, find_spec=importlib.util.find_spec, which=shutil.which,
                  run=subprocess.run) -> tuple[list[str], list[str]]:
    """(ошибки, предупреждения): пакеты реально находятся в окружении; системный tesseract на месте."""
    errors: list[str] = []
    warnings: list[str] = []
    modules = set(scan_imports(root)) | set(RUNTIME_ONLY_MODULES)
    for module in sorted(modules):
        try:
            present = find_spec(module) is not None
        except (ImportError, ValueError):
            present = False
        if not present:
            pip_name = RUNTIME_ONLY_MODULES.get(module) or IMPORT_TO_DIST.get(module, module)
            errors.append(f"модуль {module} не установлен в окружении (pip install {pip_name})")

    if which("tesseract") is None:
        errors.append("системный tesseract не найден (apt-get install tesseract-ocr; без него OCR ценников не работает)")
    else:
        try:
            out = run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20)
            have = set((out.stdout + out.stderr).split())
            missing = [lang for lang in OCR_LANGS_WANTED if lang not in have]
            if missing:
                warnings.append(f"tesseract: нет языков {', '.join(missing)} (apt-get install tesseract-ocr-<язык>)")
        except (OSError, subprocess.SubprocessError):
            warnings.append("tesseract найден, но список языков получить не удалось")
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверить зависимости проекта")
    parser.add_argument("--runtime", action="store_true", help="дополнительно проверить, что пакеты установлены")
    parser.add_argument("--root", default=str(ROOT), help="корень проекта (по умолчанию — рядом со scripts/)")
    args = parser.parse_args(argv)
    root = Path(args.root)

    problems = audit_static(root)
    warnings: list[str] = []
    if args.runtime:
        errors, warnings = audit_runtime(root)
        problems += errors

    for w in warnings:
        print(f"[WARN] {w}")
    if problems:
        print("Проблемы с зависимостями:", file=sys.stderr)
        for p in problems:
            print(f"  [FAIL] {p}", file=sys.stderr)
        return 1
    mods = len(scan_imports(root))
    print(f"Зависимости в порядке: {mods} сторонних модулей объявлены в requirements.txt"
          + (", все установлены, tesseract на месте." if args.runtime else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
