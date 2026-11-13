"""scripts/check_imports.py — аудит зависимостей. Включает «ворота»: репозиторий сам обязан проходить аудит."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import check_imports as ci

ROOT = Path(__file__).resolve().parent.parent


def _tree(tmp_path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------ ворота на самом репозитории

def test_repository_imports_are_all_declared_in_requirements():
    """Новый `import xyz` без строки в requirements.txt роняет этот тест, а не контейнер на чистой машине."""
    assert ci.audit_static(ROOT) == []


def test_both_postgres_drivers_are_declared():
    declared = ci.parse_requirements((ROOT / "requirements.txt").read_text(encoding="utf-8"))
    assert {"psycopg", "psycopg2-binary"} <= declared


def test_dockerfile_runs_the_runtime_dependency_check():
    assert "scripts.check_imports --runtime" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


# ------------------------------------------------------------------ parse_requirements

def test_parse_requirements_ignores_versions_extras_comments_and_directives():
    text = """
# комментарий
fastapi>=0.115
uvicorn[standard]>=0.30      # inline
psycopg[binary]>=3.2
Pillow==10.3.0
python_dotenv>=1.0
-r other.txt
--index-url https://example.org/simple
   pyyaml>=6.0;python_version>"3.8"
"""
    assert ci.parse_requirements(text) == {"fastapi", "uvicorn", "psycopg", "pillow", "python-dotenv", "pyyaml"}


# ------------------------------------------------------------------ scan_imports

def test_scan_finds_third_party_only_including_lazy_and_from_imports(tmp_path):
    root = _tree(tmp_path, {
        "app/__init__.py": "",
        "app/a.py": "import os\nimport numpy as np\nfrom app import b\nfrom __future__ import annotations\n",
        "app/b.py": "def f():\n    import cv2\n    from PIL import Image\n    from yaml import safe_load\n",
        "scripts/s.py": "import app\nfrom scripts import x\nimport requests.adapters\n",
    })
    found = ci.scan_imports(root)
    assert set(found) == {"numpy", "cv2", "PIL", "yaml", "requests"}
    assert found["cv2"] == {"app/b.py"}


def test_dev_only_tools_are_not_runtime_dependencies_but_only_in_their_folder_and_only_that_module(tmp_path):
    """Playwright нужен лишь снимкам экранов разработчика. Его не требуют от образа там, но не прячут, если он понадобится самому приложению."""
    root = _tree(tmp_path, {
        "app/__init__.py": "",
        "app/uses_it.py": "import playwright\n",                                     # в приложении — настоящая зависимость
        "tools/other.py": "from playwright.sync_api import sync_playwright\n",       # в другой папке инструментов — тоже не исключаем
        "tools/ui_screenshots/shots.py": "from playwright.sync_api import sync_playwright\nimport requests\nfrom PIL import Image\n",
    })
    found = ci.scan_imports(root)
    assert found["playwright"] == {"app/uses_it.py", "tools/other.py"}               # снимки экранов в списке не появились
    assert found["requests"] == {"tools/ui_screenshots/shots.py"}                     # остальные импорты того же файла проверяются как раньше
    assert found["PIL"] == {"tools/ui_screenshots/shots.py"}


def test_screenshot_tools_alone_do_not_fail_the_audit_and_do_not_need_installing_in_the_image(tmp_path):
    root = _tree(tmp_path, {"requirements.txt": "psycopg[binary]\npsycopg2-binary\n", "tools/ui_screenshots/shots.py": "from playwright.sync_api import sync_playwright\n"})
    assert ci.audit_static(root) == []
    errors, _ = ci.audit_runtime(root, find_spec=lambda name: object(), which=lambda name: None)
    assert not any("playwright" in e for e in errors)                                 # образ не обязан его содержать (сборка не падает)


def test_relative_imports_and_broken_files_are_ignored(tmp_path):
    root = _tree(tmp_path, {"app/a.py": "from . import b\nfrom .c import d\n", "app/bad.py": "def (:\n"})
    assert ci.scan_imports(root) == {}


# ------------------------------------------------------------------ audit_static

def test_missing_requirement_is_reported_with_file_and_pip_name(tmp_path):
    root = _tree(tmp_path, {
        "requirements.txt": "numpy>=1\npsycopg[binary]>=3\npsycopg2-binary>=2\n",
        "app/a.py": "import numpy\nimport cv2\n",
    })
    problems = ci.audit_static(root)
    assert len(problems) == 1
    assert "cv2" in problems[0] and "opencv-python-headless" in problems[0] and "app/a.py" in problems[0]


def test_import_name_differs_from_pip_name_is_mapped(tmp_path):
    root = _tree(tmp_path, {
        "requirements.txt": "opencv-python-headless\npillow\npyyaml\nfaiss-cpu\npython-multipart\npsycopg\npsycopg2-binary\n",
        "app/a.py": "import cv2, PIL, yaml, faiss, multipart\n",
    })
    assert ci.audit_static(root) == []


def test_postgres_driver_missing_from_requirements_is_reported_even_though_never_imported(tmp_path):
    # именно этот случай и был: код драйвер не импортирует, поэтому статический анализ импортов его не видит
    root = _tree(tmp_path, {"requirements.txt": "sqlalchemy>=2\npsycopg2-binary>=2.9\n", "app/a.py": "import sqlalchemy\n"})
    problems = ci.audit_static(root)
    assert len(problems) == 1 and "psycopg[binary]" in problems[0]


def test_missing_requirements_file_is_a_problem(tmp_path):
    assert ci.audit_static(tmp_path) == ["requirements.txt не найден"]


# ------------------------------------------------------------------ audit_runtime (окружение подменено)

def _runtime_root(tmp_path):
    return _tree(tmp_path, {"app/a.py": "import numpy\nimport cv2\n"})


def test_runtime_reports_uninstalled_modules_and_missing_tesseract(tmp_path):
    root = _runtime_root(tmp_path)
    errors, warnings = ci.audit_runtime(
        root, find_spec=lambda name: None if name in {"cv2", "psycopg"} else object(), which=lambda _: None)
    text = "\n".join(errors)
    assert "cv2" in text and "psycopg" in text and "tesseract" in text
    assert "numpy" not in text and "psycopg2" not in text
    assert warnings == []


def test_runtime_all_present_with_missing_ocr_language_is_only_a_warning(tmp_path):
    root = _runtime_root(tmp_path)
    fake_run = lambda *a, **k: SimpleNamespace(stdout="List of available languages (3):\nrus\neng\nosd\n", stderr="")
    errors, warnings = ci.audit_runtime(root, find_spec=lambda name: object(), which=lambda _: "/usr/bin/tesseract", run=fake_run)
    assert errors == []
    assert len(warnings) == 1 and "uzb" in warnings[0] and "uzb_cyrl" in warnings[0] and "rus" not in warnings[0].split("нет языков")[1]


def test_runtime_find_spec_exceptions_count_as_missing(tmp_path):
    root = _runtime_root(tmp_path)

    def boom(name):
        raise ImportError(name)

    errors, _ = ci.audit_runtime(root, find_spec=boom, which=lambda _: "/usr/bin/tesseract",
                                 run=lambda *a, **k: SimpleNamespace(stdout="rus uzb uzb_cyrl eng", stderr=""))
    assert any("numpy" in e for e in errors)


# ------------------------------------------------------------------ main

def test_main_returns_zero_for_the_repository_and_one_for_a_broken_tree(tmp_path):
    assert ci.main(["--root", str(ROOT)]) == 0
    root = _tree(tmp_path, {"requirements.txt": "numpy\n", "app/a.py": "import torch\n"})
    assert ci.main(["--root", str(root)]) == 1
