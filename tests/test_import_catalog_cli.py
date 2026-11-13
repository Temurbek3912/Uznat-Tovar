"""
scripts/import_catalog.py — режим --validate-only (без БД и моделей, реально выполняется на настоящих фото) и адаптер
SqlCatalogRepo на подставных `app.db.models` и «сессии» (SQLAlchemy в среде разработки нет).
Не проверяется: настоящая БД, DinoV2, интерактивное подтверждение и полный путь main() с загрузкой — это проводка, закрытая
статическим чекером и тестами ядра (test_catalog_import.py).
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import numpy as np
from PIL import Image

from scripts import import_catalog as ic

ROOT = Path(__file__).resolve().parent.parent


def _photo(path: Path, color=(200, 40, 40), size=240, seed=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    arr = np.clip(np.array(color) + rng.integers(-25, 26, (size, size, 3)), 0, 255).astype(np.uint8)
    Image.fromarray(arr).save(path)
    return path


def _catalog(root: Path, skus=("COLA", "FANTA"), photos=3):
    for i, sku in enumerate(skus):
        for j in range(photos):
            _photo(root / sku / f"{j}.png", (60 * (i + 1), 200 - 50 * i, 80), seed=i * 100 + j)
    return root


def _validate(root: Path, *extra):
    lines = []
    code = ic.main(["--dir", str(root), "--validate-only", *extra], out=lines.append)
    return code, "\n".join(lines)


# ----------------------------------------------------------------------- --validate-only

def test_validate_only_passes_for_a_good_folder_and_reports_counts(tmp_path):
    root = _catalog(tmp_path / "cat")
    (root / "products.csv").write_text("sku;name;brand;category;price\nCOLA;Кола;B;Напитки;99,9\nFANTA;Фанта;B;Напитки;\n", encoding="utf-8-sig")
    code, text = _validate(root)
    assert code == 0 and "Найдено товаров: 2, фото: 6" in text and "Товаров: 2, пригодных фото: 6" in text and "готова к загрузке" in text
    assert "ОШИБКИ" not in text


def test_validate_only_without_csv_warns_but_passes(tmp_path):
    code, text = _validate(_catalog(tmp_path / "cat"))
    assert code == 0 and "products.csv не найден" in text


def test_default_csv_next_to_the_photos_is_actually_used(tmp_path):
    root = _catalog(tmp_path / "cat")
    (root / "products.csv").write_text("sku,name\nCOLA,Кола\n", encoding="utf-8")                    # FANTA не описана
    code, text = _validate(root)
    assert code == 0 and "папка «FANTA» не описана в products.csv" in text and "products.csv не найден" not in text


def test_validate_only_fails_on_errors_with_a_clear_list(tmp_path):
    root = _catalog(tmp_path / "cat")
    (root / "FANTA" / "stolen.png").write_bytes((root / "COLA" / "0.png").read_bytes())             # то же фото в двух товарах
    code, text = _validate(root)
    assert code == 1 and "ОШИБКИ (1)" in text and "ТО ЖЕ фото" in text and "Исправьте ошибки" in text


def test_csv_errors_are_prefixed_and_fail_validation(tmp_path):
    root = _catalog(tmp_path / "cat")
    (root / "products.csv").write_text("sku,name,price\nCOLA,Кола,abc\n", encoding="utf-8")
    code, text = _validate(root)
    assert code == 1 and "products.csv: строка 2" in text and "не число" in text
    missing = _validate(root, "--csv", str(tmp_path / "nope.csv"))
    assert missing[0] == 1 and "нет файла" in missing[1]


def test_require_csv_makes_undescribed_folders_an_error(tmp_path):
    root = _catalog(tmp_path / "cat")
    (root / "products.csv").write_text("sku,name\nCOLA,Кола\n", encoding="utf-8")
    code, text = _validate(root, "--require-csv")
    assert code == 1 and "загрузка остановлена (--require-csv)" in text


def test_missing_folder_and_folder_without_usable_photos_fail(tmp_path):
    code, text = _validate(tmp_path / "nope")
    assert code == 1 and "нет папки" in text
    _photo(tmp_path / "tiny" / "X" / "a.png", size=30)
    code, text = _validate(tmp_path / "tiny")
    assert code == 1 and "Пригодных фото нет" in text


def test_long_warning_lists_are_truncated_with_a_count(tmp_path):
    root = tmp_path / "cat"
    for i in range(40):
        _photo(root / f"S{i:02d}" / "a.png", seed=i)                                              # у каждого по 1 фото -> 40 предупреждений
    code, text = _validate(root)
    # 40 предупреждений «всего 1 фото» + одно «products.csv не найден» = 41; показаны первые MAX_LISTED, остальные — счётчиком
    assert code == 0 and "ПРЕДУПРЕЖДЕНИЯ (41)" in text and f"и ещё {41 - ic.MAX_LISTED}" in text
    assert text.count("\n  - ") == ic.MAX_LISTED


def test_validate_only_never_touches_database_or_models(tmp_path):
    # режим должен работать там, где нет ни SQLAlchemy, ни torch: проверяем, что тяжёлые модули не импортировались
    before = {m for m in ("sqlalchemy", "torch", "transformers", "faiss") if m in sys.modules}
    _validate(_catalog(tmp_path / "cat"))
    assert {m for m in ("sqlalchemy", "torch", "transformers", "faiss") if m in sys.modules} == before


# ----------------------------------------------------------------------- адаптер БД

class FakeProduct:
    def __init__(self, sku, name, brand, category, expected_price):
        self.id = f"id-{sku}"
        self.sku, self.name, self.brand, self.category, self.expected_price = sku, name, brand, category, expected_price


class FakeSession:
    def __init__(self, rows=(), fail_commit=False):
        self.rows, self.added, self.fail_commit, self.events = list(rows), [], fail_commit, []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.events.append("commit")
        if self.fail_commit:
            raise RuntimeError("unique violation")

    def refresh(self, obj):
        self.events.append("refresh")

    def rollback(self):
        self.events.append("rollback")

    def get(self, model, pk):
        return next((r for r in self.rows if r.id == pk), None)

    def query(self, model):
        outer = self
        return types.SimpleNamespace(all=lambda: outer.rows)


def _with_fake_models(fn):
    """Подменяет app.db.models на модуль с FakeProduct на время вызова (настоящий требует SQLAlchemy)."""
    fake = types.ModuleType("app.db.models")
    fake.Product = FakeProduct
    saved = sys.modules.get("app.db.models")
    sys.modules["app.db.models"] = fake
    try:
        return fn()
    finally:
        if saved is None:
            sys.modules.pop("app.db.models", None)
        else:
            sys.modules["app.db.models"] = saved


def test_sql_repo_lists_creates_and_updates_products():
    def scenario():
        db = FakeSession([FakeProduct("A", "Имя", "Бренд", "Кат", 5.0)])
        repo = ic.SqlCatalogRepo(db)
        existing = repo.existing_products()
        assert list(existing) == ["A"] and existing["A"].id == "id-A" and existing["A"].expected_price == 5.0
        pid = repo.create_product(ic.ProductMeta("B", "Товар Б", "Br", "Cat", 12.5))
        assert pid == "id-B" and db.events == ["commit", "refresh"]
        created = db.added[0]
        assert (created.sku, created.name, created.brand, created.category, created.expected_price) == ("B", "Товар Б", "Br", "Cat", 12.5)
        repo.update_product("id-A", ic.ProductMeta("A", "Новое", "Новый", "Новая", None))
        row = db.rows[0]
        assert (row.name, row.brand, row.category, row.expected_price) == ("Новое", "Новый", "Новая", None)
    _with_fake_models(scenario)


def test_sql_repo_rolls_back_on_failure_so_the_session_stays_usable_and_reports_unknown_product():
    def scenario():
        db = FakeSession(fail_commit=True)
        repo = ic.SqlCatalogRepo(db)
        try:
            repo.create_product(ic.ProductMeta("X", "X"))
            raise AssertionError("ожидалось исключение")
        except RuntimeError as exc:
            assert "unique violation" in str(exc)
        assert db.events == ["commit", "rollback"]                                  # откат выполнен, исключение не проглочено
        db2 = FakeSession([FakeProduct("A", "n", "b", "c", None)], fail_commit=True)
        try:
            ic.SqlCatalogRepo(db2).update_product("id-A", ic.ProductMeta("A", "new"))
            raise AssertionError("ожидалось исключение")
        except RuntimeError:
            assert db2.events == ["commit", "rollback"]
        try:
            ic.SqlCatalogRepo(FakeSession()).update_product("ghost", ic.ProductMeta("G", "g"))
            raise AssertionError("ожидалось исключение")
        except LookupError as exc:
            assert "ghost" in str(exc)
    _with_fake_models(scenario)


def test_product_constructor_arguments_in_the_script_match_the_orm_columns():
    # статический чекер не видит ленивые импорты внутри методов — проверяем соответствие столбцам явно
    models = ast.parse((ROOT / "app" / "db" / "models.py").read_text(encoding="utf-8"))
    product = next(n for n in models.body if isinstance(n, ast.ClassDef) and n.name == "Product")
    columns = {s.target.id for s in product.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
    script = ast.parse((ROOT / "scripts" / "import_catalog.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(script) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Product"]
    assert calls, "в скрипте нет вызова Product(...)"
    for call in calls:
        assert {k.arg for k in call.keywords} <= columns and {"sku", "name"} <= {k.arg for k in call.keywords}
