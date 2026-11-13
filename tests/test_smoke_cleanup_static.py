"""
Статическая проверка уборки в scripts/smoke_test.py::Smoke.cleanup (только ast, без SQLAlchemy).

Smoke-проверка создаёт одного временного пользователя и в конце удаляет ровно его следы: токены, привязки и самого пользователя.
Здесь проверяется, что:
- строки, ссылающиеся на пользователя (ApiToken, UserStoreAccess), удаляются ДО него — иначе на Postgres ForeignKeyViolation
  (на sqlite в тестах проекта внешние ключи по умолчанию не проверяются, поэтому обычный тест этого не увидел бы);
- каждое групповое удаление отфильтровано по этому пользователю — уборка никогда не превращается в массовый DELETE по таблице;
- изменения фиксируются одним commit после всех удалений.

(Прежняя версия файла проверяла порядок удаления снимков полок и магазина — эта часть smoke-проверки ушла вместе с аудитом полок.)
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _cleanup() -> ast.FunctionDef:
    tree = ast.parse((ROOT / "scripts" / "smoke_test.py").read_text(encoding="utf-8"))
    smoke = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Smoke")
    return next(n for n in smoke.body if isinstance(n, ast.FunctionDef) and n.name == "cleanup")


def _chain(node: ast.AST) -> list[ast.Call]:
    """Цепочка вызовов db.query(X).filter(...).delete() -> [query, filter, delete] (снизу вверх по ast)."""
    calls = []
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        calls.append(node)
        node = node.func.value
    return list(reversed(calls))


def _bulk_deletes() -> list[tuple[int, str, list[ast.Call]]]:
    """[(строка, модель, цепочка)] для всех db.query(Модель)...delete() внутри cleanup."""
    out = []
    for node in ast.walk(_cleanup()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "delete":
            chain = _chain(node)
            if chain and chain[0].func.attr == "query" and isinstance(chain[0].func.value, ast.Name) and chain[0].func.value.id == "db":
                out.append((node.lineno, ast.unparse(chain[0].args[0]), chain))
    return sorted(out, key=lambda x: x[0])


def _db_calls() -> list[tuple[int, str, str]]:
    """[(строка, метод, первый аргумент)] для прямых вызовов db.<метод>(...) внутри cleanup."""
    calls = []
    for node in ast.walk(_cleanup()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "db"):
            calls.append((node.lineno, node.func.attr, ast.unparse(node.args[0]) if node.args else ""))
    return sorted(calls)


def test_the_helper_sees_the_expected_calls():
    models = [model for _, model, _ in _bulk_deletes()]
    assert models == ["ApiToken", "UserStoreAccess"]
    methods = [(m, a) for _, m, a in _db_calls()]
    assert ("delete", "user") in methods
    assert ("commit", "") in methods


def test_rows_referencing_the_user_are_deleted_before_the_user():
    user_delete = min(line for line, method, arg in _db_calls() if method == "delete" and arg == "user")
    for line, model, _ in _bulk_deletes():
        assert line < user_delete, f"{model} ссылается на пользователя и должен удаляться до db.delete(user)"


def test_every_bulk_delete_is_filtered_by_the_smoke_user():
    # Никакого массового DELETE: каждое групповое удаление идёт через filter(... == self.user_id).
    for line, model, chain in _bulk_deletes():
        filters = [c for c in chain if c.func.attr == "filter"]
        assert filters, f"строка {line}: удаление {model} без filter(...) стёрло бы всю таблицу"
        assert any("self.user_id" in ast.unparse(arg) for c in filters for arg in c.args), \
            f"строка {line}: удаление {model} должно быть ограничено пользователем smoke-проверки"


def test_changes_are_committed_once_after_all_deletes():
    calls = _db_calls()
    commits = [line for line, method, _ in calls if method == "commit"]
    deletes = [line for line, method, _ in calls if method == "delete"] + [line for line, _, _ in _bulk_deletes()]
    assert len(commits) == 1
    assert max(deletes) < commits[0]
