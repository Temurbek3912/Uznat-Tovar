#!/usr/bin/env python3
"""
Статический чекер для кода, который нельзя выполнить (в среде разработки нет fastapi/sqlalchemy/celery).
Только стандартная библиотека. Ловит то, что `py_compile` пропускает, а первый реальный запуск показал бы
падением:

  1. необъявленные имена (забытый импорт, опечатка) — через symtable;
  2. неиспользуемые импорты (часто след потерянного при правке кода);
  3. контракты вызовов между модулями проекта: неизвестное имя аргумента, лишние позиционные, пропущенный
     обязательный аргумент — для функций, методов (self.x(), Class(...).x()), dataclass и pydantic-моделей;
  4. поля ORM-моделей: Model(kw=...) и db.query(Model).filter_by(kw=...) с несуществующим столбцом.

Чего НЕ делает: не выводит типы (метод у переменной-экземпляра не проверяется), не понимает динамику,
не заменяет запуск. Исключение для строки — комментарий `# static-check: ignore`.

    python tools/static_check.py            # app/, scripts/ и ml/
    python tools/static_check.py app/api    # конкретные пути
"""
from __future__ import annotations

import ast
import builtins
import symtable
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

IGNORE_MARK = "static-check: ignore"
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__spec__", "__package__", "__builtins__", "__path__",
                   "__loader__", "__class__", "__qualname__", "__module__", "__annotations__"}
_BUILTINS = set(dir(builtins)) | _MODULE_DUNDERS


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


# ------------------------------------------------------------------ индекс определений проекта


@dataclass
class ClassInfo:
    module: str
    name: str
    node: ast.ClassDef
    bases: list[str]
    methods: dict[str, ast.FunctionDef] = field(default_factory=dict)
    fields: list[tuple[str, bool]] = field(default_factory=list)   # (имя, есть значение по умолчанию)
    orm_attrs: set[str] = field(default_factory=set)
    is_dataclass: bool = False
    is_pydantic: bool = False
    is_orm: bool = False
    opaque: bool = False   # есть база, про которую ничего не известно (Enum, Exception…) — конструктор не проверяем


@dataclass
class ModuleInfo:
    name: str
    path: Path
    tree: ast.Module
    source_lines: list[str]
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)   # локальное имя -> (модуль, имя)
    functions: dict[str, ast.FunctionDef] = field(default_factory=dict)
    classes: dict[str, ClassInfo] = field(default_factory=dict)


def _module_name(path: Path, root: Path) -> str:
    rel = path.resolve().relative_to(root.resolve()).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _decorator_is(dec: ast.expr, name: str) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    return (isinstance(target, ast.Name) and target.id == name) or (isinstance(target, ast.Attribute) and target.attr == name)


def _base_name(b: ast.expr) -> str:
    if isinstance(b, ast.Name):
        return b.id
    if isinstance(b, ast.Attribute):
        return b.attr
    return ""


def _has_default_in_field(value: ast.expr | None) -> bool:
    if value is None:
        return False
    if isinstance(value, ast.Constant) and value.value is Ellipsis:
        return False                                   # pydantic: `x: int = ...` — обязательное
    if isinstance(value, ast.Call) and _base_name(value.func) == "Field":
        if value.args and isinstance(value.args[0], ast.Constant) and value.args[0].value is Ellipsis:
            return False
        return bool(value.args) or any(k.arg in ("default", "default_factory") for k in value.keywords)
    return True


def _build_class(module: str, node: ast.ClassDef) -> ClassInfo:
    info = ClassInfo(module=module, name=node.name, node=node, bases=[_base_name(b) for b in node.bases])
    info.is_dataclass = any(_decorator_is(d, "dataclass") for d in node.decorator_list)
    for stmt in node.body:
        if isinstance(stmt, ast.FunctionDef):
            info.methods[stmt.name] = stmt
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            ann = ast.unparse(stmt.annotation)
            if ann.startswith("ClassVar") or "ClassVar[" in ann:
                continue
            info.fields.append((stmt.target.id, _has_default_in_field(stmt.value)))
            if ann.startswith("Mapped[") or (isinstance(stmt.value, ast.Call)
                                             and _base_name(stmt.value.func) in ("mapped_column", "relationship")):
                info.orm_attrs.add(stmt.target.id)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    if t.id == "__tablename__":
                        info.is_orm = True
                    if isinstance(stmt.value, ast.Call) and _base_name(stmt.value.func) in ("mapped_column", "relationship", "Column"):
                        info.orm_attrs.add(t.id)
    if info.orm_attrs:
        info.is_orm = True
    return info


def build_index(paths: Iterable[Path], root: Path) -> dict[str, ModuleInfo]:
    modules: dict[str, ModuleInfo] = {}
    for p in sorted(paths):
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(p))
        mod = ModuleInfo(name=_module_name(p, root), path=p, tree=tree, source_lines=src.splitlines())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for a in node.names:
                    mod.imports[a.asname or a.name] = (node.module, a.name)
            elif isinstance(node, (ast.FunctionDef,)):
                mod.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                mod.classes[node.name] = _build_class(mod.name, node)
        modules[mod.name] = mod
    # Производные признаки (pydantic / наследование ORM и dataclass) — после того как все классы известны.
    for mod in modules.values():
        for ci in mod.classes.values():
            _classify(ci, mod, modules)
    return modules


def _resolve_class(name: str, mod: ModuleInfo, modules: dict[str, ModuleInfo]) -> ClassInfo | None:
    if name in mod.classes:
        return mod.classes[name]
    if name in mod.imports:
        src_mod, src_name = mod.imports[name]
        target = modules.get(src_mod)
        if target and src_name in target.classes:
            return target.classes[src_name]
    return None


def _classify(ci: ClassInfo, mod: ModuleInfo, modules: dict[str, ModuleInfo], _depth: int = 0) -> None:
    if _depth > 10:
        return
    inherited_fields: list[tuple[str, bool]] = []
    for b in ci.bases:
        if b in ("BaseModel",):
            ci.is_pydantic = True
        elif b in ("DeclarativeBase", "Base"):
            ci.is_orm = True
        elif b in ("object", ""):
            pass
        else:
            parent = _resolve_class(b, mod, modules)
            if parent is None:
                ci.opaque = True            # Enum, Exception, str… — что именно делает база, неизвестно
            else:
                _classify(parent, modules[parent.module], modules, _depth + 1)
                ci.is_pydantic |= parent.is_pydantic
                ci.is_orm |= parent.is_orm
                ci.is_dataclass |= parent.is_dataclass and ci.is_dataclass
                ci.opaque |= parent.opaque
                inherited_fields += parent.fields
                ci.orm_attrs |= parent.orm_attrs
    if inherited_fields:
        seen = {n for n, _ in ci.fields}
        ci.fields = [f for f in inherited_fields if f[0] not in seen] + ci.fields


# ------------------------------------------------------------------ необъявленные имена и неиспользуемые импорты


def check_names(path: Path, src: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = src.splitlines()
    tree = ast.parse(src, filename=str(path))
    table = symtable.symtable(src, str(path), "exec")

    defined = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    has_star = any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names) for n in ast.walk(tree))
    referenced_globals: set[str] = set()
    undefined: set[str] = set()

    def walk(t: symtable.SymbolTable, is_module: bool) -> None:
        for s in t.get_symbols():
            nm = s.get_name()
            if not s.is_referenced():
                continue
            if is_module:
                if nm in defined:
                    referenced_globals.add(nm)
                else:
                    undefined.add(nm)
            elif s.is_global():
                referenced_globals.add(nm)
                if nm not in defined:
                    undefined.add(nm)
        for c in t.get_children():
            walk(c, False)

    walk(table, True)

    if not has_star:
        for nm in sorted(undefined - _BUILTINS):
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == nm and isinstance(node.ctx, ast.Load):
                    if IGNORE_MARK not in lines[node.lineno - 1]:
                        findings.append(Finding(str(path), node.lineno, f"необъявленное имя '{nm}'"))
                    break

    # Неиспользуемые импорты: используется, если имя встречается где угодно в AST (в т.ч. в аннотациях).
    ast_used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    exported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                exported |= {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    if path.name != "__init__.py":
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                    continue
                line = lines[node.lineno - 1]
                if "noqa" in line or IGNORE_MARK in line:
                    continue
                for a in node.names:
                    local = (a.asname or a.name).split(".")[0]
                    if local != "*" and local not in ast_used and local not in exported:
                        findings.append(Finding(str(path), node.lineno, f"неиспользуемый импорт '{local}'"))
    return findings


# ------------------------------------------------------------------ контракты вызовов


def _params(fn: ast.FunctionDef, drop_first: bool):
    a = fn.args
    pos = [(x.arg, False) for x in a.posonlyargs + a.args]
    n_def = len(a.defaults)
    pos = [(n, i >= len(pos) - n_def) for i, (n, _) in enumerate(pos)]
    if drop_first and pos:
        pos = pos[1:]
    kwonly = [(x.arg, d is not None) for x, d in zip(a.kwonlyargs, a.kw_defaults)]
    return pos, kwonly, a.vararg is not None, a.kwarg is not None


def _check_signature(call: ast.Call, label: str, pos, kwonly, has_var, has_kw, path: Path) -> list[Finding]:
    out: list[Finding] = []
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        return out                                      # *args/**kwargs в вызове — статически не проверить
    names = {n for n, _ in pos} | {n for n, _ in kwonly}
    for k in call.keywords:
        if k.arg not in names and not has_kw:
            out.append(Finding(str(path), call.lineno, f"{label}: нет параметра '{k.arg}'"))
    if len(call.args) > len(pos) and not has_var:
        out.append(Finding(str(path), call.lineno, f"{label}: передано {len(call.args)} позиционных, принимает {len(pos)}"))
    given = {n for n, _ in pos[:len(call.args)]} | {k.arg for k in call.keywords}
    missing = [n for n, d in pos + kwonly if not d and n not in given]
    if missing:
        out.append(Finding(str(path), call.lineno, f"{label}: не передано обязательное: {', '.join(missing)}"))
    return out


def _check_class_ctor(call: ast.Call, ci: ClassInfo, path: Path) -> list[Finding]:
    label = f"{ci.name}(...)"
    if ci.opaque:
        return []
    if "__init__" in ci.methods:
        pos, kwonly, var, kw = _params(ci.methods["__init__"], drop_first=True)
        return _check_signature(call, label, pos, kwonly, var, kw, path)
    if ci.is_orm:
        out = []
        if call.args:
            out.append(Finding(str(path), call.lineno, f"{label}: у ORM-модели нет позиционных аргументов"))
        if not any(k.arg is None for k in call.keywords):
            for k in call.keywords:
                if k.arg not in ci.orm_attrs:
                    out.append(Finding(str(path), call.lineno, f"{label}: у модели нет поля '{k.arg}'"))
        return out
    if ci.is_pydantic:
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            return []
        out = []
        if call.args:
            out.append(Finding(str(path), call.lineno, f"{label}: pydantic-модель принимает только именованные аргументы"))
        names = {n for n, _ in ci.fields}
        for k in call.keywords:
            if k.arg not in names:
                out.append(Finding(str(path), call.lineno, f"{label}: нет поля '{k.arg}'"))
        given = {k.arg for k in call.keywords}
        missing = [n for n, d in ci.fields if not d and n not in given]
        if missing:
            out.append(Finding(str(path), call.lineno, f"{label}: не передано обязательное поле: {', '.join(missing)}"))
        return out
    if ci.is_dataclass:
        return _check_signature(call, label, [(n, d) for n, d in ci.fields], [], False, False, path)
    return []


def _chain_root_model(call: ast.Call, mod: ModuleInfo, modules: dict[str, ModuleInfo]) -> ClassInfo | None:
    """db.query(Model).filter(...).filter_by(...) -> Model."""
    node: ast.expr = call
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "query" and node.args and isinstance(node.args[0], ast.Name):
            return _resolve_class(node.args[0].id, mod, modules)
        node = node.func.value
    return None


class _CallChecker(ast.NodeVisitor):
    def __init__(self, mod: ModuleInfo, modules: dict[str, ModuleInfo]):
        self.mod, self.modules, self.findings = mod, modules, []
        self.class_stack: list[ClassInfo] = []
        self.lines = mod.source_lines

    def visit_ClassDef(self, node: ast.ClassDef):
        ci = self.mod.classes.get(node.name) if len(self.class_stack) == 0 else None
        if ci is not None:
            self.class_stack.append(ci)
            self.generic_visit(node)
            self.class_stack.pop()
        else:
            self.generic_visit(node)

    def _resolve_func(self, name: str) -> ast.FunctionDef | None:
        if name in self.mod.functions:
            return self.mod.functions[name]
        if name in self.mod.imports:
            src_mod, src_name = self.mod.imports[name]
            t = self.modules.get(src_mod)
            if t and src_name in t.functions:
                return t.functions[src_name]
        return None

    def _method_params(self, ci: ClassInfo, name: str):
        fn = ci.methods.get(name)
        if fn is None:
            return None
        decos = {_base_name(d.func if isinstance(d, ast.Call) else d) for d in fn.decorator_list}
        if "staticmethod" in decos:
            return _params(fn, drop_first=False)
        if "property" in decos:
            return None
        return _params(fn, drop_first=True)        # self / cls

    def visit_Call(self, node: ast.Call):
        if IGNORE_MARK not in self.lines[node.lineno - 1]:
            self._check(node)
        self.generic_visit(node)

    def _check(self, node: ast.Call) -> None:
        f, path = node.func, self.mod.path
        if isinstance(f, ast.Name):
            ci = _resolve_class(f.id, self.mod, self.modules)
            if ci is not None:
                self.findings += _check_class_ctor(node, ci, path)
                return
            fn = self._resolve_func(f.id)
            if fn is not None:
                pos, kwonly, var, kw = _params(fn, drop_first=False)
                self.findings += _check_signature(node, f"{f.id}()", pos, kwonly, var, kw, path)
            return
        if not isinstance(f, ast.Attribute):
            return
        if f.attr == "filter_by":
            model = _chain_root_model(node, self.mod, self.modules)
            if model is not None and model.is_orm and not model.opaque:
                for k in node.keywords:
                    if k.arg is not None and k.arg not in model.orm_attrs:
                        self.findings.append(Finding(str(path), node.lineno, f"filter_by: у {model.name} нет поля '{k.arg}'"))
            return
        target: ClassInfo | None = None
        if isinstance(f.value, ast.Name) and f.value.id == "self" and self.class_stack:
            target = self.class_stack[-1]
        elif isinstance(f.value, ast.Call) and isinstance(f.value.func, ast.Name):
            target = _resolve_class(f.value.func.id, self.mod, self.modules)
        if target is not None:
            params = self._method_params(target, f.attr)
            if params is not None:
                pos, kwonly, var, kw = params
                self.findings += _check_signature(node, f"{target.name}.{f.attr}()", pos, kwonly, var, kw, path)


def check_calls(mod: ModuleInfo, modules: dict[str, ModuleInfo]) -> list[Finding]:
    checker = _CallChecker(mod, modules)
    checker.visit(mod.tree)
    return checker.findings


# ------------------------------------------------------------------ точка входа


def collect_files(targets: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for t in targets:
        if t.is_dir():
            files += [p for p in t.rglob("*.py") if "__pycache__" not in p.parts]
        elif t.suffix == ".py":
            files.append(t)
    return sorted(set(files))


def check_paths(targets: Iterable[Path], root: Path) -> list[Finding]:
    files = collect_files(targets)
    modules = build_index(files, root)
    findings: list[Finding] = []
    for mod in modules.values():
        src = "\n".join(mod.source_lines) + "\n"
        findings += check_names(mod.path, src)
        findings += check_calls(mod, modules)
    return sorted(set(findings), key=lambda f: (f.path, f.line, f.message))


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    args = argv if argv is not None else sys.argv[1:]
    targets = [Path(a) for a in args] or [root / "app", root / "scripts", root / "ml"]
    findings = check_paths(targets, root)
    for f in findings:
        print(f)
    print(f"\nНайдено замечаний: {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
