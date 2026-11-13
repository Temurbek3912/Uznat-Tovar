"""
Офлайн-раннер тестов для retail-shelf-audit в песочнице без интернета и
без pydantic/FastAPI/SQLAlchemy/faiss/torch/pytest.

Идея: pytest и pydantic — это ИНФРАСТРУКТУРА (тестовый фреймворк и валидатор
данных), а не бизнес-логика проекта. app/core/schemas.py не содержит никакой
логики — только объявления полей. Поэтому вместо того, чтобы пытаться
поставить настоящие pydantic/pytest (нет сети — невозможно), подставляем
лёгкие подмены ТОЛЬКО для них, и запускаем НАСТОЯЩИЙ код tests/*.py и
app/pipeline/*.py как есть, без единой правки в самом проекте.

Ограничение: специфичное поведение pydantic v2 (валидация типов при
конструировании, `Optional[PriceCheckResult]` как поле и т.п.) этим НЕ
проверяется — см. HANDOFF.md, пункт про Detection.price_check.

Запуск (из любого места, путь к проекту берётся от расположения этого файла):
    python3 tools/offline_test_harness.py

Как только в окружении появятся настоящие pytest+pydantic — этот файл больше
не нужен, используйте `pytest tests/ -v` напрямую.
"""
from __future__ import annotations

import importlib
import inspect
import pathlib
import re
import sys
import tempfile
import traceback
import types
from dataclasses import dataclass
from enum import Enum
from typing import Optional

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]  # tools/ -> корень проекта
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# 1. Мини-pytest: только то, что реально используется в tests/*.py
# ---------------------------------------------------------------------------


class Skipped(Exception):
    pass


class _Raises:
    def __init__(self, expected_exception, match=None):
        self.expected_exception = expected_exception
        self.match = match
        self.value = None  # заполняется в __exit__ — как exc_info.value у настоящего pytest.raises

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            raise AssertionError(f"DID NOT RAISE {self.expected_exception}")
        if not issubclass(exc_type, self.expected_exception):
            return False  # не тот тип исключения — пусть пробрасывается дальше как есть
        if self.match is not None and not re.search(self.match, str(exc_val)):
            raise AssertionError(f"Regex pattern {self.match!r} does not match {str(exc_val)!r}")
        self.value = exc_val
        return True


class _Approx:
    def __init__(self, value, rel=1e-6, abs_tol=1e-9):
        self.value = value
        self.rel = rel
        self.abs_tol = abs_tol

    def __eq__(self, other):
        try:
            diff = abs(other - self.value)
        except TypeError:
            return NotImplemented
        tol = max(self.abs_tol, self.rel * abs(self.value))
        return diff <= tol

    def __repr__(self):
        return f"approx({self.value!r})"


def _approx(value, rel=1e-6, abs=1e-9):  # noqa: A002 - matches pytest's real kwarg name
    return _Approx(value, rel=rel, abs_tol=abs)


def _skip(msg=""):
    raise Skipped(msg)


def _importorskip(modname, minversion=None, reason=None):
    """Как pytest.importorskip: нет модуля (fastapi/sqlalchemy/...) -> пропустить весь тест-модуль."""
    try:
        return importlib.import_module(modname)
    except ImportError:
        raise Skipped(reason or f"нет модуля {modname}")


def _fixture(*args, **kwargs):
    # Фикстуры офлайн-раннер не умеет — но модули с ними в любом случае отсекаются importorskip выше.
    if args and callable(args[0]):
        return args[0]
    return lambda func: func


class _Mark:
    @staticmethod
    def parametrize(argnames, argvalues):
        def decorator(func):
            func.__pytest_parametrize__ = (argnames, argvalues)
            return func

        return decorator

    @staticmethod
    def skipif(condition, reason=""):
        # Работает и на классе, и на функции: обеим можно присвоить произвольный атрибут.
        # Раннер (run_module) читает __pytest_skip__ до вызова — сам тест/класс не исполняется.
        def decorator(obj):
            if condition:
                obj.__pytest_skip__ = reason or "skipif"
            return obj

        return decorator


pytest_stub = types.ModuleType("pytest")
pytest_stub.raises = _Raises
pytest_stub.approx = _approx
pytest_stub.skip = _skip
pytest_stub.importorskip = _importorskip
pytest_stub.fixture = _fixture
pytest_stub.mark = _Mark()
sys.modules["pytest"] = pytest_stub

# ---------------------------------------------------------------------------
# 2. Мини-schemas: замена app/core/schemas.py без pydantic.
#    Только контейнеры данных (как в оригинале) — без валидации типов.
# ---------------------------------------------------------------------------

schemas_mod = types.ModuleType("app.core.schemas")


class DetectionStatus(str, Enum):
    MATCHED = "matched"
    UNCERTAIN = "uncertain"
    UNKNOWN = "unknown"
    EMPTY_SLOT = "empty_slot"


@dataclass
class BoundingBox:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


@dataclass
class Detection:
    bbox: BoundingBox
    detector_confidence: float
    matched_product_id: Optional[str] = None
    match_score: Optional[float] = None
    status: Optional[DetectionStatus] = None
    shelf_row: Optional[int] = None
    slot_index: Optional[int] = None
    price_check: Optional[object] = None


@dataclass
class PlanogramSlot:
    shelf_id: str
    row: int
    slot_index: int
    expected_product_id: str
    expected_facings: int = 1


@dataclass
class ComplianceViolation:
    slot: PlanogramSlot
    found_product_id: Optional[str]
    violation_type: str


schemas_mod.DetectionStatus = DetectionStatus
schemas_mod.BoundingBox = BoundingBox
schemas_mod.Detection = Detection
schemas_mod.PlanogramSlot = PlanogramSlot
schemas_mod.ComplianceViolation = ComplianceViolation

import app.core  # noqa: E402 - должен импортироваться после манипуляций с sys.path

sys.modules["app.core.schemas"] = schemas_mod
app.core.schemas = schemas_mod

# ---------------------------------------------------------------------------
# 3. Мини-коллектор/раннер тестов (замена `pytest tests/ -v`)
# ---------------------------------------------------------------------------

def _discover_test_modules() -> list[str]:
    tests_dir = REPO_ROOT / "tests"
    return sorted(f"tests.{p.stem}" for p in tests_dir.glob("test_*.py"))


TEST_MODULES = _discover_test_modules()


def _run_one(label, func, kwargs):
    try:
        func(**kwargs)
        return "PASS", label, None
    except Skipped as e:
        return "SKIP", label, str(e)
    except AssertionError as e:
        return "FAIL", label, str(e) or traceback.format_exc(limit=1)
    except Exception:
        return "ERROR", label, traceback.format_exc()


def run_module(modname):
    results = []
    try:
        mod = importlib.import_module(modname)
    except Skipped as e:
        results.append(("SKIP", modname, str(e)))
        return results
    except Exception:
        results.append(("COLLECT_ERROR", modname, traceback.format_exc()))
        return results

    for cls_name, cls in inspect.getmembers(mod, inspect.isclass):
        if not cls_name.startswith("Test"):
            continue
        class_skip = getattr(cls, "__pytest_skip__", None)
        if class_skip is not None:
            results.append(("SKIP", f"{modname}::{cls_name}", class_skip))
            continue
        instance = cls()
        for m_name, method in inspect.getmembers(instance, inspect.ismethod):
            if not m_name.startswith("test_"):
                continue
            label = f"{modname}::{cls_name}::{m_name}"
            method_skip = getattr(method.__func__, "__pytest_skip__", None)
            if method_skip is not None:
                results.append(("SKIP", label, method_skip))
                continue
            param_info = getattr(method.__func__, "__pytest_parametrize__", None)
            if param_info:
                argnames, argvalues = param_info
                names = [n.strip() for n in argnames.split(",")]
                for values in argvalues:
                    values_t = values if isinstance(values, (tuple, list)) else (values,)
                    kwargs = dict(zip(names, values_t))
                    results.append(_run_one(f"{label}[{values_t}]", method, kwargs))
            else:
                sig = inspect.signature(method)
                kwargs = {}
                if "tmp_path" in sig.parameters:
                    kwargs["tmp_path"] = pathlib.Path(tempfile.mkdtemp())
                results.append(_run_one(label, method, kwargs))

    # Тесты модульного уровня (def test_*(...) вне классов) — как их собирает настоящий pytest.
    # Раньше раннер молча пропускал их: зелёный прогон ничего не доказывал для таких файлов.
    for fn_name, fn in inspect.getmembers(mod, inspect.isfunction):
        if not fn_name.startswith("test_") or fn.__module__ != modname:
            continue
        label = f"{modname}::{fn_name}"
        fn_skip = getattr(fn, "__pytest_skip__", None)
        if fn_skip is not None:
            results.append(("SKIP", label, fn_skip))
            continue
        param_info = getattr(fn, "__pytest_parametrize__", None)
        if param_info:
            argnames, argvalues = param_info
            names = [n.strip() for n in argnames.split(",")]
            for values in argvalues:
                values_t = values if isinstance(values, (tuple, list)) else (values,)
                results.append(_run_one(f"{label}[{values_t}]", fn, dict(zip(names, values_t))))
        else:
            kwargs = {}
            if "tmp_path" in inspect.signature(fn).parameters:
                kwargs["tmp_path"] = pathlib.Path(tempfile.mkdtemp())
            results.append(_run_one(label, fn, kwargs))
    return results


def main(argv=None):
    """Без аргументов — все модули. Аргументы — подстроки имён модулей: `python tools/offline_test_harness.py start_demo pwa`
    (быстрый прогон при разработке и при проверке мутациями)."""
    wanted = list(sys.argv[1:] if argv is None else argv)
    modules = [m for m in TEST_MODULES if not wanted or any(w in m for w in wanted)]
    if wanted and not modules:
        print(f"Нет модулей, подходящих под {wanted}")
        return 2
    all_results = []
    for modname in modules:
        all_results.extend(run_module(modname))

    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0, "COLLECT_ERROR": 0}
    for status, label, detail in all_results:
        counts[status] = counts.get(status, 0) + 1
        marker = {"PASS": "ok", "FAIL": "FAIL", "ERROR": "ERROR",
                  "SKIP": "skip", "COLLECT_ERROR": "COLLECT_ERROR"}[status]
        print(f"[{marker:^14}] {label}")
        if status in ("FAIL", "ERROR", "COLLECT_ERROR"):
            print("    " + str(detail).replace("\n", "\n    "))

    total = len(all_results)
    print("\n" + "=" * 70)
    print(
        f"TOTAL={total}  passed={counts['PASS']}  failed={counts['FAIL']}  "
        f"errors={counts['ERROR']}  skipped={counts['SKIP']}  "
        f"collect_errors={counts['COLLECT_ERROR']}"
    )
    return 0 if counts["FAIL"] == 0 and counts["ERROR"] == 0 and counts["COLLECT_ERROR"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
