"""
Статическая проверка (только ast, без fastapi): в app/ не используются константы starlette.status, которые в новых версиях
переименованы или объявлены устаревшими. Рабочее правило проекта — числовые коды (`HTTPException(422, ...)`): оно не зависит
от версии Starlette. Стабильные имена (HTTP_404_NOT_FOUND, HTTP_409_CONFLICT и т.п.) этой проверкой не запрещаются.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Имена, переименованные в новых версиях Starlette (старые остаются только как устаревшие алиасы с предупреждением).
RENAMED = {
    "HTTP_413_REQUEST_ENTITY_TOO_LARGE": "HTTP_413_CONTENT_TOO_LARGE",
    "HTTP_414_REQUEST_URI_TOO_LONG": "HTTP_414_URI_TOO_LONG",
    "HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE": "HTTP_416_RANGE_NOT_SATISFIABLE",
    "HTTP_422_UNPROCESSABLE_ENTITY": "HTTP_422_UNPROCESSABLE_CONTENT",
}


def _offenders(source: str) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(source)):
        name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
        if name in RENAMED:
            found.append((node.lineno, name))
        if isinstance(node, ast.ImportFrom):
            found += [(node.lineno, a.name) for a in node.names if a.name in RENAMED]
    return found


def test_the_detector_itself_finds_attribute_name_and_import_usages():
    assert _offenders("raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY)") == [(1, "HTTP_422_UNPROCESSABLE_ENTITY")]
    assert _offenders("code = HTTP_413_REQUEST_ENTITY_TOO_LARGE") == [(1, "HTTP_413_REQUEST_ENTITY_TOO_LARGE")]
    assert _offenders("from starlette.status import HTTP_414_REQUEST_URI_TOO_LONG") == [(1, "HTTP_414_REQUEST_URI_TOO_LONG")]
    assert _offenders("status.HTTP_404_NOT_FOUND; HTTPException(422)  # HTTP_422_UNPROCESSABLE_ENTITY в комментарии") == []


def _app_sources() -> dict[str, str]:
    return {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in sorted((ROOT / "app").rglob("*.py"))}


def test_the_scan_really_covers_routers_and_entrypoints():
    # Защита от тихого сужения проверки: нарушители живут именно в app/api, а не в app/core.
    scanned = set(_app_sources())
    assert {"app/main.py", "app/worker.py", "app/api/routes_auth.py", "app/api/routes_stores.py",
            "app/api/routes_catalog.py", "app/api/routes_admin.py", "app/core/security.py"} <= scanned
    assert len(scanned) >= 40


def test_app_does_not_use_renamed_starlette_status_constants():
    problems = [f"{rel}:{line}: {name} -> число (или {RENAMED[name]})"
                for rel, source in _app_sources().items() for line, name in _offenders(source)]
    assert not problems, "\n".join(problems)
