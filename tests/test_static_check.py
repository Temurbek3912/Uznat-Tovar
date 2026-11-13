"""
tools/static_check.py: (1) сам чекер находит ошибки и не даёт ложных срабатываний — на синтетических
проектах; (2) «ворота» — код проекта (app/, scripts/, tests/, tools/, ml/) не содержит замечаний. Ворота важны именно
потому, что слой БД/HTTP/Celery нельзя выполнить в среде разработки: это единственная автоматическая страховка
от забытого импорта, опечатки в имени, неверного имени аргумента или столбца ORM.
"""
from __future__ import annotations

from pathlib import Path

from tools.static_check import check_paths

ROOT = Path(__file__).resolve().parent.parent


def _run(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return [f.message for f in check_paths([tmp_path], tmp_path)]


def _has(messages, fragment):
    return any(fragment in m for m in messages)


# ------------------------------------------------------------------ имена и импорты

def test_undefined_name_and_forgotten_import_are_found(tmp_path):
    msgs = _run(tmp_path, {"m.py": "def f():\n    return helper(1)\n\ndef g():\n    tot = 1\n    return totl\n"})
    assert _has(msgs, "необъявленное имя 'helper'") and _has(msgs, "необъявленное имя 'totl'")


def test_valid_scoping_constructs_produce_no_false_positives(tmp_path):
    src = '''
from __future__ import annotations
import os
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from decimal import Decimal

COUNT = 0

class A:
    LIMIT = 3
    doubled = [x * 2 for x in range(LIMIT)]
    def m(self, d: Decimal) -> "A":
        global COUNT
        COUNT += 1
        total = sum(v for v in range(3))
        def inner():
            return total + COUNT
        try:
            os.getcwd()
        except OSError as exc:
            print(exc)
        with open("f") as fh:
            fh.read()
        if (n := inner()) > 1:
            return self
        import json
        return json.dumps(n) and (lambda q: q + n)
'''
    assert _run(tmp_path, {"m.py": src}) == []


def test_unused_import_found_but_annotation_only_noqa_all_future_and_init_are_fine(tmp_path):
    msgs = _run(tmp_path, {
        "a.py": "import os\nimport sys\nfrom decimal import Decimal\n\ndef f(x: Decimal) -> None:\n    sys.exit(x)\n",
        "b.py": "from __future__ import annotations\nimport re  # noqa: F401\nfrom x import y\n__all__ = ['y']\n",
        "pkg/__init__.py": "from .a import thing\n",
    })
    assert msgs == ["неиспользуемый импорт 'os'"]


def test_ignore_mark_suppresses_a_finding(tmp_path):
    assert _run(tmp_path, {"m.py": "def f():\n    return later()  # static-check: ignore\n"}) == []
    # и для контракта вызова (отдельная ветка кода), причём соседний вызов без метки по-прежнему ловится
    msgs = _run(tmp_path, {
        "lib.py": "def work(a):\n    return a\n",
        "use.py": "from lib import work\nwork(1, 2)  # static-check: ignore\nwork(1, 2)\n",
    })
    assert len(msgs) == 1 and "передано 2 позиционных" in msgs[0]


# ------------------------------------------------------------------ контракты вызовов функций

_FUNCS = '''
def work(a, b, c=1, *, mode="x"):
    return a

def anything(a, **kw):
    return a

def varargs(*args):
    return args
'''


def test_function_call_contract_errors(tmp_path):
    msgs = _run(tmp_path, {
        "lib.py": _FUNCS,
        "use.py": "from lib import work\nwork(1, 2, 3, 4)\nwork(1, 2, nope=3)\nwork(1)\nwork(1, 2, mode='y')\n",
    })
    assert _has(msgs, "передано 4 позиционных, принимает 3")
    assert _has(msgs, "нет параметра 'nope'")
    assert _has(msgs, "не передано обязательное: b")
    assert len(msgs) == 3                                      # корректный вызов work(1, 2, mode='y') — без замечаний


def test_calls_with_star_args_kwargs_and_open_signatures_are_not_flagged(tmp_path):
    msgs = _run(tmp_path, {
        "lib.py": _FUNCS,
        "use.py": ("from lib import work, anything, varargs\nargs = (1, 2)\nkw = {}\n"
                   "work(*args)\nwork(1, **kw)\nanything(1, whatever=2, other=3)\nvarargs(1, 2, 3)\n"),
    })
    assert msgs == []


# ------------------------------------------------------------------ методы и классы

_SVC = '''
class Svc:
    def __init__(self, db, flag=False):
        self.db = db
    def run(self, x, *, fast=False):
        return self.helper(x)
    def helper(self, x):
        return x
    def bad_inner(self):
        return self.helper(1, 2)
    @staticmethod
    def make(a, b):
        return a
    @classmethod
    def build(cls, a):
        return cls(a)
    @property
    def prop(self):
        return 1
'''


def test_method_and_constructor_contracts(tmp_path):
    msgs = _run(tmp_path, {
        "svc.py": _SVC,
        "use.py": ("from svc import Svc\n"
                   "Svc()\nSvc(1, 2, 3)\nSvc(1, nope=1)\nSvc(1).run()\nSvc(1).run(1, 2)\nSvc(1).run(1, fast=True)\n"
                   "Svc.make(1, 2)\nSvc(1).prop\n"),
    })
    assert _has(msgs, "Svc(...): не передано обязательное: db")
    assert _has(msgs, "Svc(...): передано 3 позиционных, принимает 2")
    assert _has(msgs, "Svc(...): нет параметра 'nope'")
    assert _has(msgs, "Svc.run(): не передано обязательное: x")
    assert _has(msgs, "Svc.run(): передано 2 позиционных, принимает 1")
    assert _has(msgs, "Svc.helper(): передано 2 позиционных, принимает 1")        # self.helper(1, 2) внутри класса
    assert len(msgs) == 6                                                          # всё остальное — корректно


# ------------------------------------------------------------------ dataclass / pydantic

_SCHEMAS = '''
from dataclasses import dataclass, field
from pydantic import BaseModel, Field
from typing import ClassVar, Optional

@dataclass
class Box:
    x: int
    y: int = 0
    tags: list = field(default_factory=list)
    KIND: ClassVar[str] = "box"

class Item(BaseModel):
    name: str
    qty: int = 1
    note: Optional[str] = None
    maybe: Optional[int]
    price: float = Field(default=0.0)
    code: str = Field(...)

class Child(Item):
    extra: str = "e"

class Ell(BaseModel):
    v: int = ...
'''


def test_dataclass_and_pydantic_constructor_contracts(tmp_path):
    msgs = _run(tmp_path, {
        "schemas.py": _SCHEMAS,
        "use.py": ("from schemas import Box, Item, Child, Ell\n"
                   "Box(1)\nBox(1, 2, [])\nBox(x=1, tags=[])\nBox()\nBox(1, z=2)\n"
                   "Item(name='a', maybe=None, code='c')\n"
                   "Item('a')\nItem(name='a', code='c')\nItem(name='a', maybe=1, code='c', bogus=1)\n"
                   "Child(name='a', maybe=1, code='c', extra='z')\nChild(name='a', maybe=1, code='c', nope=1)\n"
                   "Ell(v=1)\nEll()\n"),
    })
    assert _has(msgs, "Ell(...): не передано обязательное поле: v")           # `v: int = ...` — обязательное
    assert sum("Ell(" in m for m in msgs) == 1
    assert _has(msgs, "Box(...): не передано обязательное: x")
    assert _has(msgs, "Box(...): нет параметра 'z'")
    assert _has(msgs, "Item(...): pydantic-модель принимает только именованные")
    assert _has(msgs, "Item(...): не передано обязательное поле: maybe")      # Optional без значения в pydantic v2 — обязательное
    assert _has(msgs, "Item(...): нет поля 'bogus'")
    assert _has(msgs, "Child(...): нет поля 'nope'")
    assert not any("Box(...): не передано обязательное: y" in m or "'KIND'" in m for m in msgs)


# ------------------------------------------------------------------ ORM

_MODELS = '''
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

class Store(Base):
    __tablename__ = "stores"
    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()
    shelves = relationship("Shelf")

class Shelf(Base):
    __tablename__ = "shelves"
    id: Mapped[str] = mapped_column(primary_key=True)
    store_id: Mapped[str] = mapped_column()
'''


def test_orm_constructor_and_filter_by_contracts(tmp_path):
    msgs = _run(tmp_path, {
        "models.py": _MODELS,
        "use.py": ("from models import Store, Shelf\n"
                   "def f(db):\n"
                   "    Store(name='a')\n    Store(nme='a')\n    Store('a')\n    Shelf(store_id='s', id='1')\n"
                   "    db.query(Shelf).filter_by(store_id='s').first()\n"
                   "    db.query(Shelf).filter_by(store='s').first()\n"
                   "    (db.query(Store)\n       .filter(Store.id == '1')\n       .filter_by(shelves=None, naem='x').all())\n"
                   "    db.query(Store).filter_by(name='ok', id='1')\n"),
    })
    assert _has(msgs, "Store(...): у модели нет поля 'nme'")
    assert _has(msgs, "Store(...): у ORM-модели нет позиционных аргументов")
    assert _has(msgs, "filter_by: у Shelf нет поля 'store'")
    assert _has(msgs, "filter_by: у Store нет поля 'naem'")
    assert len(msgs) == 4                                      # relationship 'shelves' и все верные вызовы — без замечаний


def test_classes_with_unknown_bases_are_not_checked(tmp_path):
    msgs = _run(tmp_path, {
        "e.py": ("from enum import Enum\nfrom dataclasses import dataclass\nfrom pydantic import BaseModel\n"
                 "from outside import Mixin, External\n"
                 "class Color(str, Enum):\n    RED = 'r'\n"
                 "class Boom(Exception):\n    pass\n"
                 "@dataclass\nclass D(External):\n    own: int = 0\n"            # поля базы неизвестны
                 "class P(Mixin, BaseModel):\n    own: int = 0\n"),                # то же для pydantic
        "use.py": ("from e import Color, Boom, D, P\nColor('r')\nBoom('a', 'b', 'c')\n"
                   "D(inherited_field=1)\nD(1, 2, 3)\nP(inherited_field=1)\n"),
    })
    assert msgs == []


# ------------------------------------------------------------------ ворота

def test_project_code_has_no_static_findings():
    findings = check_paths([ROOT / "app", ROOT / "scripts", ROOT / "tests", ROOT / "tools", ROOT / "ml"], ROOT)
    assert [str(f) for f in findings] == []
