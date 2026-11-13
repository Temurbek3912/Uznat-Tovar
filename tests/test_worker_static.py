"""
Статические проверки app/worker.py и роутов — без celery/fastapi (только ast), поэтому реально
выполняются в любой среде.

Зачем: тесты API подменяют app.worker заглушкой, и потерянный декоратор @celery_app.task
(реальный баг после F12: add_catalog_product_task стала обычной функцией, `.delay` -> AttributeError)
через них пройти не может.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _tree(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"))


def _is_task_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    return isinstance(target, ast.Attribute) and target.attr == "task"


def _celery_task_names() -> set[str]:
    return {
        n.name for n in _tree("app/worker.py").body
        if isinstance(n, ast.FunctionDef) and any(_is_task_decorator(d) for d in n.decorator_list)
    }


def test_known_tasks_are_registered_celery_tasks():
    tasks = _celery_task_names()
    # Ровно эти задачи: каталог, распознавание и (с 2026-11-07, этап 4) восстановление индекса при запуске воркера. Задач аудита
    # (снимки полок, переиндексация по ревью) больше нет, и случайно вернуться они не должны — поэтому проверяется равенство.
    assert tasks == {"add_catalog_product_task", "add_catalog_photos_task", "identify_product_task", "recover_index_task"}


def test_every_bind_true_task_function_is_actually_decorated():
    # Функция с первым параметром `self` на верхнем уровне worker.py — это bind=True задача; без декоратора
    # она «обычная функция с self», и .delay() у неё нет.
    undecorated = [
        n.name for n in _tree("app/worker.py").body
        if isinstance(n, ast.FunctionDef) and n.args.args and n.args.args[0].arg == "self"
        and not any(_is_task_decorator(d) for d in n.decorator_list)
    ]
    assert undecorated == []


def test_everything_called_with_delay_in_api_is_a_real_task():
    tasks = _celery_task_names()
    checked = 0
    for path in sorted((ROOT / "app" / "api").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "delay" and isinstance(node.func.value, ast.Name)):
                checked += 1
                assert node.func.value.id in tasks, f"{path.name}: {node.func.value.id}.delay() — не Celery-задача"
    assert checked >= 3   # add_catalog_product, add_catalog_photos, identify — защита от пустой проверки


def _func(name: str) -> ast.FunctionDef:
    return next(n for n in _tree("app/worker.py").body if isinstance(n, ast.FunctionDef) and n.name == name)


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    t = handler.type
    if t is None:
        return ["<bare>"]
    nodes = t.elts if isinstance(t, ast.Tuple) else [t]
    return [n.id if isinstance(n, ast.Name) else n.attr for n in nodes if isinstance(n, (ast.Name, ast.Attribute))]


def test_human_readable_failures_are_not_retried_before_the_generic_handler():
    # Плохое фото / дубль артикула: повтор через 15 секунд ×3 не поможет, а человек ждал бы минуту до понятного сообщения.
    # Поэтому обработчик CatalogAddError (поднимает исключение дальше, БЕЗ self.retry) стоит РАНЬШЕ общего `except Exception`.
    for task in ("add_catalog_product_task", "add_catalog_photos_task"):
        tries = [n for n in ast.walk(_func(task)) if isinstance(n, ast.Try)]
        assert tries, task
        names = [h for t in tries for h in t.handlers]
        order = [_handler_names(h) for h in names]
        flat = [n for group in order for n in group]
        assert "CatalogAddError" in flat and "Exception" in flat, (task, flat)
        assert flat.index("CatalogAddError") < flat.index("Exception"), (task, flat)
        catalog_handler = next(h for h in names if "CatalogAddError" in _handler_names(h))
        assert not any(isinstance(n, ast.Attribute) and n.attr == "retry" for n in ast.walk(catalog_handler)), task


def test_identify_task_never_retries_and_always_removes_its_temporary_frame():
    fn = _func("identify_product_task")
    deco = next(d for d in fn.decorator_list if _is_task_decorator(d))
    assert isinstance(deco, ast.Call) and not any(k.arg in {"max_retries", "autoretry_for"} for k in deco.keywords)
    assert not any(isinstance(n, ast.Attribute) and n.attr == "retry" for n in ast.walk(fn))
    finals = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    assert finals and any(isinstance(n, ast.Attribute) and n.attr == "unlink" for f in finals for stmt in f.finalbody for n in ast.walk(stmt))


def test_uploaded_references_are_resolved_only_through_the_checked_helper():
    # _download_image не должен собирать путь из ссылки сам: только resolve_upload_ref (проверка «../» и символьных ссылок).
    fn = _func("_download_image")
    called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert {"is_upload_ref", "resolve_upload_ref", "uploads_root"} <= called
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open" for n in ast.walk(fn))


def test_both_tasks_that_add_uploaded_photos_record_them_in_the_journal_after_indexing():
    # Без записи в журнал скрипт восстановления индекса (scripts/reindex_uploads.py) не знал бы, какие фото уже в индексе.
    # 2026-11-07: индексация фото к существующему товару — catalog_add.index_photos (порядок «индекс -> журнал» внутри неё
    # закреплён tests/test_catalog_add.py); здесь — что журнал передан именно ей и вызывается после неё.
    for task, indexing_call in (("add_catalog_product_task", "add_product"), ("add_catalog_photos_task", "index_photos")):
        calls = [n for n in ast.walk(_func(task)) if isinstance(n, ast.Call)]
        names = [(c.func.id if isinstance(c.func, ast.Name) else c.func.attr) for c in calls if isinstance(c.func, (ast.Name, ast.Attribute))]
        assert "_note_indexed" in names and indexing_call in names, task
        assert names.index("_note_indexed") > names.index(indexing_call), f"{task}: запись в журнал должна идти ПОСЛЕ добавления в индекс"


def test_barcodes_are_linked_after_the_product_exists_and_a_failed_link_never_loses_the_product():
    calls = [n for n in ast.walk(_func("add_catalog_product_task")) if isinstance(n, ast.Call)]
    names = [(c.func.id if isinstance(c.func, ast.Name) else c.func.attr) for c in calls if isinstance(c.func, (ast.Name, ast.Attribute))]
    assert "_link_barcodes" in names and names.index("_link_barcodes") > names.index("add_product")
    helper = _func("_link_barcodes")
    handlers = [h for n in ast.walk(helper) if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers and all(not any(isinstance(n, ast.Raise) for n in ast.walk(h)) for h in handlers)   # сбой привязки не пробрасывается
    assert any(isinstance(n, ast.Attribute) and n.attr == "rollback" for h in handlers for n in ast.walk(h))


def test_worker_init_never_raises_and_never_swaps_components_itself():
    # Исключение из обработчика сигнала Celery проглатывает («Signal handler raised») и объявляет ready — поэтому инициализация
    # ловит сбои внутри ml_runtime и НЕ бросает; подмена детектора/эмбеддера — только там и только по флагу.
    init = _func("_init_ml_components")
    assert not any(isinstance(n, ast.Raise) for n in ast.walk(init))
    source = (ROOT / "app" / "worker.py").read_text(encoding="utf-8")
    assert "HeuristicContourDetector" not in source and "RandomDebugEmbedder" not in source and "YoloShelfDetector" not in source


def test_preflight_runs_in_the_main_process_and_stops_the_worker_with_systemexit():
    fn = _func("_preflight_worker")
    decos = [ast.unparse(d) for d in fn.decorator_list]
    assert decos == ["worker_init.connect"]
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    assert raises and all(isinstance(r.exc, ast.Call) and getattr(r.exc.func, "id", "") == "SystemExit" for r in raises)   # BaseException: Celery его не проглотит


def test_every_ml_task_checks_readiness_before_working():
    needs = {"add_catalog_product_task": "_require_ml", "add_catalog_photos_task": "_require_ml", "identify_product_task": "_require_ml"}
    for task, guard in needs.items():
        called = [n.func.id for n in ast.walk(_func(task)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert guard in called, task
        assert "_init_ml_components" not in called, f"{task}: молча перезапускать инициализацию из задачи нельзя — только _require_ml (раз в минуту)"
    for task in ("add_catalog_product_task", "add_catalog_photos_task", "identify_product_task"):
        calls = [n.func.id for n in ast.walk(_func(task)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert "_ml_unavailable" in calls, f"{task}: сбой компонентов должен стать понятным отказом, а не «Не получилось»"
