"""
Статическая проверка app/api/routes_catalog.py (только ast: без fastapi, поэтому выполняется в любой среде).

Зачем. Права на маршруты каталога — защита от того, что мерчандайзер или аналитик добавит/изменит товар. Тесты API, проверяющие
это через HTTP, в среде разработки не запускались (нет fastapi/sqlalchemy), поэтому таблица «маршрут -> право» закреплена здесь:
забытая зависимость require_permission или подмена права CATALOG_WRITE на CATALOG_READ ломает этот тест.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXPECTED = {
    ("GET", "/products"): "CATALOG_READ",
    ("POST", "/products"): "CATALOG_WRITE",
    ("POST", "/products/upload"): "CATALOG_WRITE",
    ("POST", "/products/{product_id}/photos"): "CATALOG_WRITE",
    ("PATCH", "/products/{product_id}"): "CATALOG_WRITE",
    ("GET", "/products/tasks/{task_id}"): "CATALOG_WRITE",
    ("GET", "/products/{product_id}/photo"): "CATALOG_READ",
    ("GET", "/products/{product_id}/images"): "CATALOG_READ",            # фото товара из базы (шаг Р-7)
    ("PATCH", "/products/{product_id}/images/{image_id}"): "CATALOG_WRITE",  # вывести из эталонов / витринное — только admin
    ("POST", "/products/{product_id}/status"): "CATALOG_WRITE",              # убрать в архив / вернуть — только admin
    ("POST", "/identify"): "CATALOG_READ",
}


def _routes() -> dict[tuple[str, str], tuple[ast.FunctionDef, list[str]]]:
    tree = ast.parse((ROOT / "app" / "api" / "routes_catalog.py").read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "router" and dec.args and isinstance(dec.args[0], ast.Constant)):
                perms = [n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                         and n.value.id == "Permission"]
                found[(dec.func.attr.upper(), dec.args[0].value)] = (node, perms)
    return found


def test_every_catalog_route_requires_exactly_the_expected_permission():
    routes = _routes()
    assert set(routes) == set(EXPECTED), f"маршруты каталога изменились: {sorted(set(routes) ^ set(EXPECTED))} — обновите таблицу и тесты"
    for key, (node, perms) in routes.items():
        assert perms == [EXPECTED[key]], f"{key}: права {perms}, ожидалось {EXPECTED[key]}"
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "require_permission"]
        assert len(calls) == 1, f"{key}: зависимость require_permission должна быть одна"


def test_writing_routes_are_admin_only_reading_routes_are_open_to_every_role_with_catalog_read():
    from app.core.security import ROLE_PERMISSIONS, Permission, Role

    writers = [r for r, p in ROLE_PERMISSIONS.items() if Permission.CATALOG_WRITE in p]
    assert writers == [Role.ADMIN]
    readers = {r for r, p in ROLE_PERMISSIONS.items() if Permission.CATALOG_READ in p}
    assert {Role.MERCHANDISER, Role.REGIONAL_MANAGER, Role.ANALYST, Role.ADMIN} <= readers


def test_task_status_route_is_declared_before_routes_with_a_product_id_segment():
    # /products/tasks/{task_id} должен стоять раньше /products/{product_id}/...: иначе при добавлении новых маршрутов
    # «tasks» могло бы быть принято за product_id.
    tree = ast.parse((ROOT / "app" / "api" / "routes_catalog.py").read_text(encoding="utf-8"))
    order = []
    for node in tree.body:
        for dec in getattr(node, "decorator_list", []):
            if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                order.append((dec.func.attr.upper(), dec.args[0].value))
    gets = [path for method, path in order if method == "GET"]
    assert gets.index("/products/tasks/{task_id}") < gets.index("/products/{product_id}/photo")


def test_accepting_routes_answer_202_and_their_handlers_are_plain_def_so_file_reads_do_not_block_the_event_loop():
    routes = _routes()
    for key in (("POST", "/products"), ("POST", "/products/upload"), ("POST", "/products/{product_id}/photos")):
        node = routes[key][0]
        deco = next(d for d in node.decorator_list if isinstance(d, ast.Call))
        assert any(k.arg == "status_code" and getattr(k.value, "value", None) == 202 for k in deco.keywords), key
    for key in (("POST", "/products/upload"), ("POST", "/products/{product_id}/photos"), ("PATCH", "/products/{product_id}")):
        assert isinstance(routes[key][0], ast.FunctionDef), f"{key}: синхронный def (файлы читаются блокирующе — FastAPI уведёт в пул потоков)"
    # «Узнать товар» (D-10а): кадр читается из запроса в память асинхронно (read_frame), а блокирующая часть — штрихкод, очередь,
    # ожидание воркера — уходит в пул потоков, цикл событий не блокируется.
    identify = ast.unparse(routes[("POST", "/identify")][0])
    assert isinstance(routes[("POST", "/identify")][0], ast.AsyncFunctionDef)
    assert "await read_frame(" in identify and "await run_in_threadpool(run_identify" in identify


def test_user_facing_texts_do_not_leak_internals_and_status_codes_are_plain_numbers():
    text = (ROOT / "app" / "api" / "routes_catalog.py").read_text(encoding="utf-8")
    assert "status.HTTP_" not in text                       # единый стиль проекта: числовые коды (tests/test_status_codes_static.py)
    # исключение целиком в ответ не отдаём; только наши CatalogAddError/PhotoError (запасной текст причины у фото — тоже наш PhotoError)
    assert "str(exc)" not in text.replace("detail=str(exc)", "").replace('"reason": reason(exc.code, exc.params) or str(exc)}', "")


def test_identify_waits_for_the_worker_with_a_timeout_and_answers_504_on_timeout():
    text = (ROOT / "app" / "api" / "routes_catalog.py").read_text(encoding="utf-8")
    assert "task.get(timeout=IDENTIFY_WAIT_SECONDS, propagate=False, disable_sync_subtasks=False)" in text
    assert 'ApiError(504, "identify_timeout"' in text and 'ApiError(503, "queue_unavailable"' in text
