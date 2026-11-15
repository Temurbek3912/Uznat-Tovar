"""
Исполняемая проверка ЛОГИКИ маршрутов app/api/routes_catalog.py без fastapi, sqlalchemy и Celery.

Настоящий код маршрутов (порядок проверок, коды ответов, тексты, что уходит в очередь, что пишется на диск) вызывается напрямую как
обычные функции; вместо библиотек подставляются крошечные заглушки, а ТОЛЬКО на время теста (sys.modules потом возвращается как был,
так что на машине с настоящими fastapi/sqlalchemy другие тесты не затрагиваются).

Что НЕ проверяется (это делает tests/test_catalog_upload_api.py там, где fastapi есть): разбор multipart-запроса самим FastAPI,
настоящий SQL, зависимости/авторизация через HTTP. Права на маршруты закреплены tests/test_catalog_routes_static.py.
"""
from __future__ import annotations

import asyncio
import importlib
import io
import sys
import types
from pathlib import Path

import numpy as np
from PIL import Image

MISSING = object()
FAKED = ["fastapi", "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.exc", "pydantic", "app.api.deps", "app.core.config", "app.db.models",
         "app.db.session", "app.worker", "app.api.routes_catalog", "app.core.errors",
         "app.services.product_search"]   # поиск каталога (общий с покупателем): заново, на подставных sqlalchemy и моделях


def _fake_require_permission(perm):
    """Заглушка app.api.deps.require_permission: как и настоящая, возвращает ВЫЗЫВАЕМУЮ зависимость (а не кортеж) —
    настоящий FastAPI, если он подставится, проверяет это при создании маршрута и падает на не-функции."""
    def dependency():
        return None
    dependency.permission = perm        # в отладчике видно, какое право просил маршрут
    return dependency


def _snapshot(names):
    """Запоминает, что лежит в sys.modules под этими именами и что записано в пакете-родителе (атрибут вроде `app.api.deps`)."""
    snap = {}
    for name in names:
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name) if parent_name else None
        attr = getattr(parent, child, MISSING) if parent is not None else MISSING
        snap[name] = (sys.modules.get(name, MISSING), attr)
    return snap


def _put_back(snap):
    """Возвращает sys.modules и атрибуты пакетов ровно такими, какими их запомнил _snapshot()."""
    for name, (module, _attr) in snap.items():
        if module is MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    for name, (_module, attr) in snap.items():
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name) if parent_name else None
        if parent is None:
            continue
        if attr is MISSING:
            vars(parent).pop(child, None)     # import_module() записал в пакет заглушечный модуль — убираем
        else:
            setattr(parent, child, attr)


# ------------------------------------------------------------------ заглушки

# Настоящая HTTPException Starlette (FastAPI наследует её же; Starlette есть и в облачной среде): общая ApiError
# (app/core/errors.py) — её подкласс, поэтому заглушка FastAPI ловит и ApiError, и прежние HTTPException маршрутов.
from starlette.exceptions import HTTPException  # noqa: E402


class FakeBaseModel:
    def __init__(self, **kw):
        for name in getattr(type(self), "__annotations__", {}):
            setattr(self, name, kw.pop(name, getattr(type(self), name, None)))
        assert not kw, f"лишние поля {kw}"

    def model_dump(self):
        return {n: getattr(self, n) for n in getattr(type(self), "__annotations__", {})}


class NormExpr:
    """Как replace(…lower(столбец)…) из app/core/text_search.sql_normalized: сравнение по той же нормализации."""
    def __init__(self, col):
        self.col = col

    def like(self, pattern, escape=None):
        from app.core import text_search
        inner = pattern[1:-1] if pattern.startswith("%") and pattern.endswith("%") else pattern
        needle = inner.replace(escape + "%", "%").replace(escape + "_", "_").replace(escape * 2, escape) if escape else inner
        return lambda row: needle in text_search.normalize(str(getattr(row, self.col.name) or ""))


class FakeFunc:
    """Заглушка sqlalchemy.func для поиска: lower(col) и replace(expr, a, b) дают одно выражение нормализации."""
    @staticmethod
    def lower(col):
        return NormExpr(col)

    @staticmethod
    def replace(expr, _src, _dst):
        return expr


class Col:
    def __init__(self, name, owner=None):
        self.name, self.owner = name, owner

    def ilike(self, pattern):
        needle = pattern.strip("%").lower()
        return lambda row: needle in str(getattr(row, self.name) or "").lower()

    def in_(self, values):
        values = list(values)
        return lambda row: getattr(row, self.name) in values

    def desc(self):
        return ("desc", self.name)

    def __ne__(self, other):
        return lambda row: getattr(row, self.name) != other

    def __eq__(self, other):                                  # Product.status == "active" (миграция 0006)
        return lambda row: getattr(row, self.name) == other

    __hash__ = object.__hash__


class FakeProduct:
    id = sku = name = brand = category = expected_price = created_at = status = version = updated_at = None   # столбцы ниже

    def __init__(self, sku, name, brand="X", category="Напитки", expected_price=None, id=None, created_at=0):
        self.id = id or f"id-{sku}"
        self.sku, self.name, self.brand, self.category, self.expected_price, self.created_at = sku, name, brand, category, expected_price, created_at
        self.status, self.version, self.updated_at = "active", 1, None


class FakeBarcode:
    barcode = product_id = None

    def __init__(self, barcode, product_id):
        self.barcode, self.product_id = barcode, product_id


class FakeProductImage:
    """Запись о фото (product_images): в этих тестах таблица пуста — товары без записей, фото считаются по папке, как раньше."""
    id = product_id = sha256 = role = is_active = source_ref = storage_path = created_at = None


for _cls, _names in ((FakeProduct, "id sku name brand category expected_price created_at status version updated_at"), (FakeBarcode, "barcode product_id"),
                     (FakeProductImage, "id product_id sha256 role is_active source_ref storage_path created_at")):
    for _n in _names.split():
        setattr(_cls, _n, Col(_n, _cls))


class FakeQuery:
    def __init__(self, rows, cols):
        self.rows, self.cols = list(rows), cols

    def filter(self, *preds):
        return FakeQuery([r for r in self.rows if all(p(r) for p in preds)], self.cols)

    def order_by(self, *keys):
        rows = list(self.rows)
        for key in reversed(keys):
            name, rev = (key[1], True) if isinstance(key, tuple) else (key.name, False)
            rows.sort(key=lambda r: getattr(r, name), reverse=rev)
        return FakeQuery(rows, self.cols)

    def offset(self, n):
        return FakeQuery(self.rows[n:], self.cols)

    def limit(self, n):
        return FakeQuery(self.rows[:n], self.cols)

    def all(self):
        if len(self.cols) == 1 and self.cols[0] in (FakeProduct, FakeProductImage):
            return self.rows
        return [types.SimpleNamespace(**{c.name: getattr(r, c.name) for c in self.cols}) for r in self.rows]


class FakeIntegrityError(RuntimeError):
    """Как sqlalchemy.exc.IntegrityError (подставляется в заглушку sqlalchemy.exc)."""


class FakeDB:
    """Две «таблицы»; commit() ведёт себя как база: повторный штрих-код -> IntegrityError, откат сбрасывает ожидающее и, как
    настоящая сессия, возвращает поля товаров к последнему зафиксированному состоянию. before_commit — «параллельный запрос»,
    успевающий записать что-то между проверками маршрута и его commit."""

    def __init__(self):
        self.products, self.barcodes, self.images = [], [], []
        self.commits, self.rollbacks = 0, 0
        self._pending_add, self._pending_delete = [], []
        self._saved: dict[int, dict] = {}
        self.before_commit = None

    def _remember(self):
        for p in self.products:
            self._saved.setdefault(id(p), dict(vars(p)))

    def _table(self, owner):
        if owner is FakeProductImage:
            return self.images
        return self.products if owner is FakeProduct else self.barcodes

    def query(self, *cols):
        self._remember()
        owner = cols[0] if isinstance(cols[0], type) else cols[0].owner
        return FakeQuery(self._table(owner), cols)

    def get(self, model, ident):
        self._remember()
        key = "id" if model in (FakeProduct, FakeProductImage) else "barcode"
        return next((r for r in self._table(model) if getattr(r, key) == ident), None)

    def add(self, obj):
        self._pending_add.append(obj)

    def delete(self, obj):
        self._pending_delete.append(obj)

    def commit(self):
        if self.before_commit is not None:
            hook, self.before_commit = self.before_commit, None
            hook(self)
        try:
            for obj in self._pending_add:
                if isinstance(obj, FakeBarcode) and any(b.barcode == obj.barcode for b in self.barcodes):
                    raise FakeIntegrityError("UNIQUE constraint failed: product_barcodes.barcode")
            self.barcodes.extend(o for o in self._pending_add if isinstance(o, FakeBarcode))
            self.products.extend(o for o in self._pending_add if isinstance(o, FakeProduct))
            for obj in self._pending_delete:
                self._table(type(obj)).remove(obj)
        finally:
            self._pending_add, self._pending_delete = [], []
        self._saved = {id(p): dict(vars(p)) for p in self.products}
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        self._pending_add, self._pending_delete = [], []
        for p in self.products:
            if id(p) in self._saved:
                vars(p).clear()
                vars(p).update(self._saved[id(p)])

    def refresh(self, obj):
        pass


class FakeAsync:
    def __init__(self, result, ident):
        self.id, self._result = ident, result

    def get(self, timeout=None, propagate=True):
        if isinstance(self._result, TimeoutError):
            raise self._result
        return self._result


class Upload:
    def __init__(self, data: bytes, filename="p.jpg"):
        self.file, self.filename = io.BytesIO(data), filename


def jpeg(seed=1, w=400, h=300) -> bytes:
    rng = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(0, 255, (h, w, 3), dtype=np.uint8)).save(buf, "JPEG")
    return buf.getvalue()


class Env:
    pass


def load(tmp_path: Path):
    """Подменяет библиотеки заглушками, импортирует настоящий routes_catalog; возвращает (env, restore).

    Тест проверяет только ЛОГИКУ маршрутов, поэтому fastapi, pydantic и sqlalchemy подменяются всегда — есть они в системе или нет:
    тогда результат один и тот же в песочнице и в Docker.

    Если что-то сломается посреди загрузки, sys.modules возвращается как был ещё ДО того, как исключение дойдёт до теста. Иначе
    заглушки (например, app.api.deps без ensure_store_access) остались бы в памяти и ломали все тесты, идущие дальше.
    """
    env = Env()
    env.schemas_patched = None
    saved = _snapshot(FAKED)

    def restore():
        _put_back(saved)
        if env.schemas_patched is not None:
            old, _new = env.schemas_patched
            env.schemas_patched = None
            if old is None:
                sys.modules.pop("app.core.schemas", None)
            else:
                del old.ProductIn

    try:
        _build_env(env, tmp_path)
    except BaseException:
        restore()
        raise
    return env, restore


def _build_env(env, tmp_path: Path):
    # Настоящие app.core.schemas (им нужен pydantic) подгружаем ДО подмены библиотек: потом pydantic станет заглушкой.
    schemas = sys.modules.get("app.core.schemas")
    if schemas is None:
        try:
            schemas = importlib.import_module("app.core.schemas")           # настоящая (есть pydantic) — со своим ProductIn
        except ImportError:
            schemas = None

    for n in FAKED:
        sys.modules.pop(n, None)
    env.queued, env.queue_down, env.identify_answer = [], False, None
    env.queue_options = []
    env.settings = types.SimpleNamespace(max_upload_mb=1, media_root=str(tmp_path / "media"))
    env.db = FakeDB()

    def inject(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    class APIRouter:
        def __init__(self, **kw):
            pass

        def _deco(self, *a, **k):
            return lambda fn: fn
        get = post = patch = put = delete = _deco

    inject("fastapi", APIRouter=APIRouter, Depends=lambda x=None: x, File=lambda default=None, **k: default, Request=object,
           Form=lambda default=None, **k: default, HTTPException=HTTPException, UploadFile=Upload,
           Response=lambda content=b"", media_type=None, headers=None: types.SimpleNamespace(content=content, media_type=media_type, headers=headers))
    inject("pydantic", BaseModel=FakeBaseModel)
    inject("sqlalchemy", or_=lambda *preds: (lambda row: any(p(row) for p in preds)), func=FakeFunc())
    inject("sqlalchemy.orm", Session=object)
    inject("sqlalchemy.exc", IntegrityError=FakeIntegrityError)
    env.HTTPException = sys.modules["fastapi"].HTTPException

    inject("app.api.deps", require_permission=_fake_require_permission)
    inject("app.core.config", get_settings=lambda: env.settings, resolve_project_path=lambda p: Path(p))
    inject("app.db.models", Product=FakeProduct, ProductBarcodeORM=FakeBarcode, ProductImage=FakeProductImage,
           PRODUCT_ACTIVE="active", PRODUCT_ARCHIVED="archived")
    inject("app.db.session", get_db=lambda: env.db)

    def make_task(name, result=None):
        def delay(*args):
            if env.queue_down:
                raise ConnectionError("redis down")
            env.queued.append((name, args))
            return FakeAsync(result() if callable(result) else result, f"{name}-{len(env.queued)}")

        def apply_async(args=(), kwargs=None, **options):
            # как Celery: позиционные аргументы задачи — в args; параметры постановки (expires и т. п.) запоминаем отдельно
            env.queue_options.append((name, options))
            return delay(*args)
        return types.SimpleNamespace(delay=delay, apply_async=apply_async)

    inject("app.worker", add_catalog_product_task=make_task("add_product"), add_catalog_photos_task=make_task("add_photos"),
           identify_product_task=make_task("identify", lambda: env.identify_answer),
           celery_app=types.SimpleNamespace(AsyncResult=lambda tid: types.SimpleNamespace(status="PENDING", ready=lambda: False, result=None)))

    # ProductIn: настоящая (если pydantic был) или из раннера; в крайнем случае — минимальная заглушка
    if schemas is None or not hasattr(schemas, "ProductIn"):
        class ProductIn(FakeBaseModel):
            sku: str
            name: str
            brand: str
            category: str
            expected_price: float | None = None
            reference_image_urls: list = None
            barcodes: list = None
        env_schemas = schemas or types.ModuleType("app.core.schemas")
        env_schemas.ProductIn = ProductIn
        env.schemas_patched = (schemas, env_schemas)
        sys.modules["app.core.schemas"] = env_schemas

    env.routes = importlib.import_module("app.api.routes_catalog")


def with_env(tmp_path):
    return load(tmp_path)


# ------------------------------------------------------------------ сама изоляция (страж от «загрязнения» соседних тестов)

def test_fake_permission_dependency_is_callable_like_the_real_one():
    assert callable(_fake_require_permission("catalog:read"))


def test_restore_puts_every_replaced_module_back(tmp_path):
    before = _snapshot(FAKED)
    env, restore = with_env(tmp_path)
    try:
        assert sys.modules["app.api.deps"] is not before["app.api.deps"][0]      # на время теста подмена и правда была
        assert hasattr(sys.modules["app.api.routes_catalog"], "upload_product")
    finally:
        restore()
    assert _snapshot(FAKED) == before


def test_a_failed_load_does_not_leave_fakes_behind(tmp_path):
    before = _snapshot(FAKED)
    real_import = importlib.import_module

    def broken_import(name, package=None):
        if name == "app.api.routes_catalog":
            raise RuntimeError("имитация сбоя при импорте routes_catalog")
        return real_import(name, package)

    importlib.import_module = broken_import
    try:
        try:
            with_env(tmp_path)
            failed = False
        except RuntimeError:
            failed = True
    finally:
        importlib.import_module = real_import
    assert failed, "load() должен был упасть, иначе проверка ничего не проверяет"
    assert _snapshot(FAKED) == before


def status_of(env, fn, *args, **kw):
    try:
        fn(*args, **kw)
    except env.HTTPException as exc:
        return exc.status_code, exc.detail
    raise AssertionError("ожидалась HTTPException")


def upload(env, **kw):
    params = dict(name="Кола 0,5 л", price="", brand="", category="", files=[Upload(jpeg(1)), Upload(jpeg(2))], db=env.db, _user=None)
    params.update(kw)
    return env.routes.upload_product(**params)


# ------------------------------------------------------------------ новый товар

def test_upload_builds_sku_saves_files_and_queues_exactly_one_task(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        body = upload(env, name="  Кола  0,5 л ", price="9 900", brand="BrandCola")
        assert body["sku"] == "KOLA-0.5-L" and body["name"] == "Кола 0,5 л" and body["photos"] == 2 and body["status"] == "queued"
        (task, args), = env.queued
        payload = args[0]
        assert task == "add_product" and payload["sku"] == "KOLA-0.5-L" and payload["brand"] == "BrandCola"
        assert payload["category"] == "Без категории" and payload["expected_price"] == 9900.0
        assert len(payload["reference_image_urls"]) == 2 and all(r.startswith("upload:KOLA-0.5-L/") for r in payload["reference_image_urls"])
        names = sorted(p.name for p in (tmp_path / "media" / "catalog-uploads" / "KOLA-0.5-L").iterdir())
        assert [n[:4] for n in names] == ["001-", "002-"]
        assert body["task_id"] == "add_product-1"
    finally:
        restore()


def test_upload_free_sku_when_taken_and_default_brand_when_blank(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products.append(FakeProduct("KOLA-0.5-L", "Совсем другое"))
        assert upload(env)["sku"] == "KOLA-0.5-L-2"
        assert env.queued[0][1][0]["brand"] == "Не указан"
    finally:
        restore()


def test_upload_rejects_bad_forms_before_touching_disk_or_queue(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        cases = [(dict(name="   "), "Напишите название"), (dict(price="abc"), "Цена"), (dict(price="-5"), "Цена"),
                 (dict(files=[]), "хотя бы одно фото"), (dict(files=[Upload(jpeg(i)) for i in range(9)]), "не больше 8"),
                 (dict(brand="я" * 61), "бренд"), (dict(name="я" * 121), "название")]
        for kw, fragment in cases:
            code, detail = status_of(env, env.routes.upload_product, **{
                **dict(name="Кола", price="", brand="", category="", files=[Upload(jpeg(1))], db=env.db, _user=None), **kw})
            assert code == 422 and fragment in detail, (kw, code, detail)
        assert env.queued == [] and not (tmp_path / "media").exists()
    finally:
        restore()


def test_upload_names_the_bad_photo_and_leaves_nothing_behind(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        code, detail = status_of(env, env.routes.upload_product, name="Кола", price="", brand="", category="",
                                 files=[Upload(jpeg(1)), Upload(b"definitely not a picture")], db=env.db, _user=None)
        assert code == 422 and detail.startswith("Фото 2:") and "Не удалось открыть" in detail
        assert env.queued == [] and not (tmp_path / "media").exists()
    finally:
        restore()


def test_upload_refuses_files_over_the_size_limit_with_413(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        code, detail = status_of(env, env.routes.upload_product, name="Кола", price="", brand="", category="",
                                 files=[Upload(b"\xff\xd8" + b"0" * (1024 * 1024 + 10))], db=env.db, _user=None)
        assert code == 413 and "1 МБ" in detail and env.queued == []
    finally:
        restore()


def test_duplicate_name_gives_409_pointing_at_the_existing_product(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products.append(FakeProduct("KOLA-0.5-L", "Кола 0,5 л", id="existing"))
        code, detail = status_of(env, env.routes.upload_product, name="кола 0.5 Л", price="", brand="", category="",
                                 files=[Upload(jpeg())], db=env.db, _user=None)
        assert code == 409 and detail["product_id"] == "existing" and detail["product_name"] == "Кола 0,5 л" and "уже есть" in detail["message"]
        assert env.queued == [] and not (tmp_path / "media").exists()
    finally:
        restore()


def test_queue_outage_gives_503_with_a_plain_hint(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.queue_down = True
        code, detail = status_of(env, env.routes.upload_product, name="Кола", price="", brand="", category="",
                                 files=[Upload(jpeg())], db=env.db, _user=None)
        assert code == 503 and "docker compose ps" in detail
    finally:
        restore()


def test_retry_after_failure_reuses_the_same_files_instead_of_duplicating(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.queue_down = True
        status_of(env, env.routes.upload_product, name="Кола", price="", brand="", category="", files=[Upload(jpeg(1))], db=env.db, _user=None)
        env.queue_down = False
        body = env.routes.upload_product(name="Кола", price="", brand="", category="", files=[Upload(jpeg(1))], db=env.db, _user=None)
        assert len(list((tmp_path / "media" / "catalog-uploads" / body["sku"]).iterdir())) == 1
    finally:
        restore()


# ------------------------------------------------------------------ фото к существующему

def test_add_photos_queues_refs_and_dedupes_identical_files(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products.append(FakeProduct("COLA", "Кола", id="pid"))
        body = env.routes.add_product_photos(product_id="pid", files=[Upload(jpeg(1)), Upload(jpeg(2)), Upload(jpeg(2))], db=env.db, _user=None)
        assert body["photos"] == 3 and body["name"] == "Кола" and body["product_id"] == "pid"
        (task, args), = env.queued
        assert task == "add_photos" and args[0] == "pid" and len(args[1]) == 3 and args[1][1] == args[1][2]
        assert len(list((tmp_path / "media" / "catalog-uploads" / "COLA").iterdir())) == 2
        assert status_of(env, env.routes.add_product_photos, product_id="nope", files=[Upload(jpeg())], db=env.db, _user=None)[0] == 404
        assert status_of(env, env.routes.add_product_photos, product_id="pid", files=[Upload(b"junk")], db=env.db, _user=None)[0] == 422
        assert len(env.queued) == 1
    finally:
        restore()


# ------------------------------------------------------------------ правка

def test_patch_changes_name_and_price_keeps_sku_and_never_half_applies(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        cola = FakeProduct("COLA", "Кола", expected_price=1000.0, id="c")
        env.db.products += [cola, FakeProduct("FANTA", "Фанта", id="f")]
        Update = env.routes.ProductUpdate
        out = env.routes.update_product("c", Update(name="  Кола 0,5 л ", expected_price="9 900"), env.db, None)
        assert (out.name, out.expected_price, out.sku) == ("Кола 0,5 л", 9900.0, "COLA") and env.db.commits == 1
        out = env.routes.update_product("c", Update(clear_price=True), env.db, None)
        assert out.expected_price is None and out.name == "Кола 0,5 л"
        code, detail = status_of(env, env.routes.update_product, "c", Update(name="фанта"), env.db, None)
        assert code == 409 and detail["product_name"] == "Фанта" and cola.name == "Кола 0,5 л"
        code, detail = status_of(env, env.routes.update_product, "c", Update(name="Новое имя", expected_price="abc"), env.db, None)
        commits = env.db.commits
        assert code == 422 and "Цена" in detail
        assert cola.name == "Кола 0,5 л" and env.db.commits == commits          # название НЕ применилось, ничего не сохранено
        assert status_of(env, env.routes.update_product, "nope", Update(name="X"), env.db, None)[0] == 404
        assert env.routes.update_product("c", Update(name="Кола 0,5 л"), env.db, None).name == "Кола 0,5 л"     # своё же имя — не повтор
        assert env.routes.update_product("c", Update(brand="  ", category=""), env.db, None).category == "Без категории"
    finally:
        restore()


# ------------------------------------------------------------------ список и фото

def test_list_pages_sorts_searches_and_counts_photos(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products += [FakeProduct("B", "Бета", id="b", created_at=2, expected_price=5.0), FakeProduct("A", "Альфа", id="a", created_at=1),
                            FakeProduct("C", "Гамма", id="c", created_at=3)]
        env.routes.add_product_photos(product_id="b", files=[Upload(jpeg(1)), Upload(jpeg(2))], db=env.db, _user=None)
        listing = lambda **kw: asyncio.run(env.routes.search_products(**{**dict(q="", limit=20, offset=0, sort="sku", db=env.db, _user=None), **kw}))
        assert [r.sku for r in listing()] == ["A", "B", "C"]
        assert [r.sku for r in listing(sort="new")] == ["C", "B", "A"]
        assert [r.sku for r in listing(limit=1, offset=1)] == ["B"]
        assert [r.sku for r in listing(q="альф")] == ["A"] and listing(q="нет такого") == []
        b = next(r for r in listing() if r.sku == "B")
        assert (b.photo_count, b.expected_price) == (2, 5.0) and next(r for r in listing() if r.sku == "A").photo_count == 0
        assert len(listing(limit=10_000)) == 3 and len(listing(limit=0)) == 1 and listing(offset=-5)[0].sku == "A"      # границы
    finally:
        restore()


def test_photo_endpoint_thumbnail_full_cache_and_404s(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products.append(FakeProduct("P1", "Один", id="p"))
        get = lambda **kw: env.routes.product_photo(**{**dict(product_id="p", size="thumb", n=0, db=env.db, _user=None), **kw})
        assert status_of(env, get)[0] == 404                                      # фото ещё нет
        env.routes.add_product_photos(product_id="p", files=[Upload(jpeg(1, 1200, 900))], db=env.db, _user=None)
        thumb, full = get(), get(size="full")
        assert thumb.media_type == "image/jpeg" and max(Image.open(io.BytesIO(thumb.content)).size) == 240       # exif-ok
        assert max(Image.open(io.BytesIO(full.content)).size) == 1200 and len(full.content) > len(thumb.content)   # exif-ok
        assert list((tmp_path / "media" / "catalog-uploads" / "P1").glob("thumb-001.jpg"))
        assert get().content == thumb.content                                       # из кэша
        assert status_of(env, get, n=5)[0] == 404 and status_of(env, get, n=-1)[0] == 404
        assert status_of(env, get, product_id="nope")[0] == 404
        assert len(env.routes.list_photo_files(env.routes._photos_dir(), "P1")) == 1            # миниатюра не считается фото
    finally:
        restore()


# ------------------------------------------------------------------ узнать товар

def _frames(env):
    from app.core.frame_store import get_frame_store
    return get_frame_store()


def _no_frame_files(tmp_path):
    folder = tmp_path / "media" / "identify-tmp"
    return not folder.exists() or not any(folder.iterdir())


def test_identify_returns_workers_answer_and_the_frame_never_touches_the_disk(tmp_path):
    """D-10а: кадр — только в памяти. В очередь уходит номер кадра («frame:…»), а не файл; после ответа кадра нет нигде."""
    env, restore = with_env(tmp_path)
    try:
        env.identify_answer = {"status": "matched", "product": {"name": "Кола"}}
        out = env.routes.run_identify(env.db, jpeg(1))
        assert out["product"]["name"] == "Кола"
        (task, args), = env.queued
        assert task == "identify" and args[0].startswith("frame:")
        assert _no_frame_files(tmp_path) and not (tmp_path / "media").exists()
        assert _frames(env).take(args[0].split(":", 1)[1]) is None          # API удалил кадр сразу после ответа
    finally:
        restore()


def test_identify_errors_are_plain_and_distinguishable(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        call = lambda data=None: env.routes.run_identify(env.db, data if data is not None else jpeg())
        code, detail = status_of(env, call, b"junk")
        assert code == 422 and "Не удалось открыть" in detail
        from app.services.catalog_upload import PhotoError
        env.identify_answer = PhotoError("Кадр не читается — попробуйте ещё раз.")
        assert status_of(env, call)[0] == 422
        env.identify_answer = TimeoutError("нет ответа")
        code, detail = status_of(env, call)
        assert code == 504 and "docker compose ps" in detail
        env.identify_answer = RuntimeError("boom")
        code, detail = status_of(env, call)
        assert code == 500 and "boom" not in detail
        env.queue_down = True
        assert status_of(env, call)[0] == 503
        env.queue_down = False
        assert _no_frame_files(tmp_path)
    finally:
        restore()


def test_identify_task_expires_when_the_worker_does_not_take_it_in_time(tmp_path):
    """Воркер остановлен или занят: задание не должно выполниться после того, как клиент ушёл."""
    env, restore = with_env(tmp_path)
    try:
        env.identify_answer = {"status": "unknown", "product": None}
        env.routes.run_identify(env.db, jpeg())
        (name, options), = env.queue_options
        assert name == "identify" and options.get("expires") == env.routes.IDENTIFY_WAIT_SECONDS
    finally:
        restore()


def test_frame_is_removed_from_memory_even_when_the_worker_did_not_answer(tmp_path):
    """Не дождались воркера (504) или очередь недоступна (503): кадр удаляется сразу, а не ждёт срока в 2 минуты (D-10а)."""
    env, restore = with_env(tmp_path)
    try:
        env.identify_answer = TimeoutError("воркер не ответил")
        assert status_of(env, lambda: env.routes.run_identify(env.db, jpeg(2)))[0] == 504
        (_task, args), = env.queued
        assert _frames(env).take(args[0].split(":", 1)[1]) is None and _no_frame_files(tmp_path)
        env.queue_down = True
        before = _frames(env).count() if hasattr(_frames(env), "count") else 0
        assert status_of(env, lambda: env.routes.run_identify(env.db, jpeg(3)))[0] == 503
        assert (_frames(env).count() if hasattr(_frames(env), "count") else 0) == before
    finally:
        restore()


def test_frame_store_that_writes_to_disk_is_refused_and_nothing_is_queued(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        from app.core import frame_store as fs

        class Persistent:
            def config_get(self, name):
                return {"save": "3600 1", "appendonly": "no"} if name == "save" else {"appendonly": "yes"}
        old = fs._store
        fs.set_frame_store(fs.RedisFrameStore(Persistent()))
        try:
            code, _detail = status_of(env, lambda: env.routes.run_identify(env.db, jpeg(4)))
            assert code == 503 and env.queued == []
        finally:
            fs.set_frame_store(old)
    finally:
        restore()


# ------------------------------------------------------------------ штрих-коды

EAN_A, EAN_B, EAN_C = "4780001234562", "5901234123457", "9780201379624"


def barcode_jpeg(code, W=1000, H=600, frac=0.55, seed=1) -> bytes:
    """Фото «упаковки» со штрих-кодом посередине (серый шумный фон)."""
    import cv2

    from app.services import barcode as bc
    rng = np.random.default_rng(seed)
    bars = cv2.cvtColor(bc.render(code, module=4, height=140), cv2.COLOR_GRAY2RGB)
    k = int(W * frac) / bars.shape[1]
    bars = cv2.resize(bars, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    frame = (150 + rng.normal(0, 10, (H, W, 1))).clip(0, 255).astype(np.uint8).repeat(3, axis=2)
    h, w = bars.shape[:2]
    frame[(H - h) // 2:(H - h) // 2 + h, (W - w) // 2:(W - w) // 2 + w] = bars
    buf = io.BytesIO()
    Image.fromarray(frame).save(buf, "JPEG", quality=92)
    return buf.getvalue()


def make_params(env, **kw):
    base = dict(name="Кола", price="", brand="", category="", barcode="", files=[Upload(jpeg(1))], db=env.db, _user=None)
    base.update(kw)
    return base


def test_upload_with_typed_barcode_validates_checksum_and_passes_it_to_the_worker(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        body = env.routes.upload_product(**make_params(env, barcode="4 780001 234562"))
        assert body["barcodes"] == [EAN_A] and env.queued[0][1][0]["barcodes"] == [EAN_A]
        env.queued.clear()
        code, detail = status_of(env, env.routes.upload_product, **make_params(env, name="Фанта", barcode="4780001234563"))
        assert code == 422 and "Контрольная цифра" in detail and env.queued == []
        code, detail = status_of(env, env.routes.upload_product, **make_params(env, name="Фанта", barcode="12345"))
        assert code == 422 and "5 цифр" in detail
    finally:
        restore()


def test_upload_refuses_a_barcode_that_already_belongs_to_another_product(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products.append(FakeProduct("FANTA", "Фанта", id="fanta"))
        env.db.barcodes.append(FakeBarcode(EAN_A, "fanta"))
        code, detail = status_of(env, env.routes.upload_product, **make_params(env, barcode=EAN_A))
        assert code == 409 and detail["product_id"] == "fanta" and "«Фанта»" in detail["message"] and EAN_A in detail["message"]
        assert env.queued == [] and not (tmp_path / "media").exists()
    finally:
        restore()


def test_upload_reads_a_barcode_from_a_photo_and_reports_it(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        body = env.routes.upload_product(**make_params(env, files=[Upload(jpeg(1)), Upload(barcode_jpeg(EAN_B))]))
        assert body["barcodes"] == [EAN_B] and env.queued[0][1][0]["barcodes"] == [EAN_B]
        assert {"photo": None, "message": f"Найден штрих-код {EAN_B} — привязан к товару."} in body["warnings"]
        env.queued.clear()
        typed = env.routes.upload_product(**make_params(env, name="Лимонад", barcode=EAN_C, files=[Upload(barcode_jpeg(EAN_B, seed=2))]))
        assert typed["barcodes"] == [EAN_C]                                  # введённый код главнее найденного на фото
    finally:
        restore()


def test_a_barcode_on_a_photo_that_belongs_to_another_product_is_a_warning_not_an_error(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products.append(FakeProduct("FANTA", "Фанта", id="fanta"))
        env.db.barcodes.append(FakeBarcode(EAN_B, "fanta"))
        body = env.routes.upload_product(**make_params(env, files=[Upload(barcode_jpeg(EAN_B))]))
        assert body["barcodes"] == [] and env.queued[0][1][0]["barcodes"] == []             # товар всё равно добавлен
        assert any("«Фанта»" in w["message"] and "не привязан" in w["message"] for w in body["warnings"])
    finally:
        restore()


def test_add_photos_links_a_barcode_found_on_the_photo(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products.append(FakeProduct("COLA", "Кола", id="cola"))
        body = env.routes.add_product_photos(product_id="cola", files=[Upload(barcode_jpeg(EAN_A))], db=env.db, _user=None)
        assert body["barcodes"] == [EAN_A] and [b.barcode for b in env.db.barcodes] == [EAN_A] and env.db.barcodes[0].product_id == "cola"
        again = env.routes.add_product_photos(product_id="cola", files=[Upload(barcode_jpeg(EAN_A, seed=3))], db=env.db, _user=None)
        assert again["barcodes"] == [] and len(env.db.barcodes) == 1                       # тот же код у того же товара — молча
        env.db.products.append(FakeProduct("FANTA", "Фанта", id="fanta"))
        other = env.routes.add_product_photos(product_id="fanta", files=[Upload(barcode_jpeg(EAN_A, seed=4))], db=env.db, _user=None)
        assert other["barcodes"] == [] and any("«Кола»" in w["message"] for w in other["warnings"]) and len(env.db.barcodes) == 1
    finally:
        restore()


def test_a_barcode_claimed_by_a_parallel_request_does_not_break_adding_photos(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products.append(FakeProduct("COLA", "Кола", id="cola"))
        original_owners = env.routes._owners
        env.routes._owners = lambda db, codes: {}                           # проверка «свободен» прошла, но к коммиту код уже занят
        env.db.barcodes.append(FakeBarcode(EAN_A, "someone"))
        body = env.routes.add_product_photos(product_id="cola", files=[Upload(barcode_jpeg(EAN_A))], db=env.db, _user=None)
        env.routes._owners = original_owners
        assert body["barcodes"] == [] and env.db.rollbacks == 1 and len(env.db.barcodes) == 1
        assert any("только что привязан" in w["message"] for w in body["warnings"]) and env.queued[0][0] == "add_photos"
    finally:
        restore()


def test_patch_adds_and_removes_barcodes_with_checks(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products += [FakeProduct("COLA", "Кола", id="cola"), FakeProduct("FANTA", "Фанта", id="fanta")]
        env.db.barcodes.append(FakeBarcode(EAN_B, "fanta"))
        Update = env.routes.ProductUpdate
        out = env.routes.update_product("cola", Update(add_barcode="4 780001 234562"), env.db, None)
        assert out.barcodes == [EAN_A]
        assert env.routes.update_product("cola", Update(add_barcode=EAN_A), env.db, None).barcodes == [EAN_A]       # повтор — без дубля
        code, detail = status_of(env, env.routes.update_product, "cola", Update(add_barcode=EAN_B), env.db, None)
        assert code == 409 and detail["product_name"] == "Фанта"
        code, detail = status_of(env, env.routes.update_product, "cola", Update(add_barcode="4780001234563"), env.db, None)
        assert code == 422 and "Контрольная цифра" in detail
        code, detail = status_of(env, env.routes.update_product, "cola", Update(remove_barcode=EAN_B), env.db, None)
        assert code == 404 and "нет" in detail                                      # чужой код отвязать нельзя
        out = env.routes.update_product("cola", Update(remove_barcode=EAN_A), env.db, None)
        assert out.barcodes == [] and [b.barcode for b in env.db.barcodes] == [EAN_B]
        # ошибка в штрих-коде не оставляет применённым новое название
        env.routes.update_product("cola", Update(add_barcode=EAN_A), env.db, None)
        status_of(env, env.routes.update_product, "cola", Update(name="Новое имя", add_barcode="000"), env.db, None)
        assert next(p for p in env.db.products if p.id == "cola").name == "Кола"
    finally:
        restore()


def test_list_shows_barcodes_sorted(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products += [FakeProduct("A", "Альфа", id="a"), FakeProduct("B", "Бета", id="b")]
        env.db.barcodes += [FakeBarcode(EAN_C, "a"), FakeBarcode(EAN_A, "a"), FakeBarcode(EAN_B, "b")]
        rows = asyncio.run(env.routes.search_products(q="", limit=20, offset=0, sort="sku", db=env.db, _user=None))
        assert [(r.sku, r.barcodes) for r in rows] == [("A", sorted([EAN_A, EAN_C])), ("B", [EAN_B])]
    finally:
        restore()


def test_identify_answers_from_a_known_barcode_without_asking_the_worker(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products.append(FakeProduct("COLA", "Кола 0,5 л", brand="BrandCola", expected_price=9900.0, id="cola"))
        env.db.barcodes.append(FakeBarcode(EAN_A, "cola"))
        out = env.routes.run_identify(env.db, barcode_jpeg(EAN_A))
        assert out["status"] == "matched" and out["by"] == "barcode" and out["score"] == 1.0 and out["demo_mode"] is False
        assert out["product"] == {"id": "cola", "name": "Кола 0,5 л", "brand": "BrandCola", "expected_price": 9900.0, "score": 1.0}
        assert out["barcode"] == {"code": EAN_A, "known": True} and env.queued == []              # воркер не нужен
        env.queue_down = True                                                                     # и при недоступной очереди код читается
        assert env.routes.run_identify(env.db, barcode_jpeg(EAN_A, seed=5))["product"]["name"] == "Кола 0,5 л"
    finally:
        restore()


def test_identify_with_an_unknown_barcode_still_recognises_by_look_and_reports_the_code(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.identify_answer = {"status": "matched", "product": {"id": "cola", "name": "Кола"}, "candidates": []}
        out = env.routes.run_identify(env.db, barcode_jpeg(EAN_C))
        assert out["by"] == "look" and out["barcode"] == {"code": EAN_C, "known": False} and out["product"]["name"] == "Кола"
        assert env.queued[0][0] == "identify"
        plain = env.routes.run_identify(env.db, jpeg(1))
        assert plain["by"] == "look" and plain["barcode"] is None
        env.db.products.append(FakeProduct("X", "Икс", id="x"))
        env.db.barcodes.append(FakeBarcode(EAN_C, "ghost"))                                       # код привязан к исчезнувшему товару
        ghost = env.routes.run_identify(env.db, barcode_jpeg(EAN_C, seed=6))
        assert ghost["by"] == "look"                                                              # не выдаём ответ про несуществующий товар
    finally:
        restore()


# ------------------------------------------------------------------ 2026-11-07: гонка привязки штрихкода в PATCH /products/{id}

def test_patch_barcode_taken_by_a_parallel_request_gives_409_not_500_and_applies_nothing(tmp_path):
    """Проверка «код свободен» прошла, но до commit другой запрос привязал тот же код к другому товару. Уникальность держит база
    (IntegrityError при commit); раньше исключение уходило наверх — ошибка сервера 500. Теперь 409 с именем владельца,
    и ни одно поле запроса не применено (запрос атомарен)."""
    env, restore = with_env(tmp_path)
    try:
        env.db.products += [FakeProduct("COLA", "Кола", id="cola"), FakeProduct("FANTA", "Фанта", id="fanta")]
        env.db.before_commit = lambda db: db.barcodes.append(FakeBarcode(EAN_A, "fanta"))
        Update = env.routes.ProductUpdate
        code, detail = status_of(env, env.routes.update_product, "cola", Update(name="Кола Zero", add_barcode=EAN_A), env.db, None)
        assert code == 409 and detail["product_name"] == "Фанта" and EAN_A in detail["message"]
        cola = next(p for p in env.db.products if p.id == "cola")
        assert cola.name == "Кола" and [(b.barcode, b.product_id) for b in env.db.barcodes] == [(EAN_A, "fanta")]
        assert env.db.rollbacks == 1
    finally:
        restore()


def test_patch_same_barcode_attached_to_the_same_product_by_a_parallel_request_still_succeeds(tmp_path):
    """Двойное нажатие: оба запроса привязывают один код к одному товару. Второй не должен получить ни 500, ни 409 — код уже
    у этого товара; остальные изменения запроса применяются."""
    env, restore = with_env(tmp_path)
    try:
        env.db.products += [FakeProduct("COLA", "Кола", id="cola")]
        env.db.before_commit = lambda db: db.barcodes.append(FakeBarcode(EAN_A, "cola"))
        Update = env.routes.ProductUpdate
        out = env.routes.update_product("cola", Update(name="Кола Zero", add_barcode=EAN_A), env.db, None)
        assert out.name == "Кола Zero" and out.barcodes == [EAN_A]
        assert [(b.barcode, b.product_id) for b in env.db.barcodes] == [(EAN_A, "cola")]
    finally:
        restore()


def test_patch_other_integrity_conflicts_give_409_with_a_clear_message(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        env.db.products += [FakeProduct("COLA", "Кола", id="cola")]

        def boom(db):
            raise FakeIntegrityError("другое ограничение")
        env.db.before_commit = boom
        code, detail = status_of(env, env.routes.update_product, "cola", env.routes.ProductUpdate(name="Кола 2"), env.db, None)
        assert code == 409 and "повторите" in detail["message"]
        assert next(p for p in env.db.products if p.id == "cola").name == "Кола"
    finally:
        restore()


# ------------------------------------------------------------------ коды ошибок (app/core/errors.py)

def error_of(env, fn, *args, **kw):
    try:
        fn(*args, **kw)
    except env.HTTPException as exc:
        return exc
    raise AssertionError("ожидалась HTTPException")


def test_errors_carry_a_code_and_params_and_keep_the_russian_text(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        missing = error_of(env, env.routes.add_product_photos, product_id="nope", files=[Upload(jpeg(1))], db=env.db, _user=None)
        assert (missing.status_code, missing.code, missing.detail) == (404, "product_not_found", "Товар не найден.")

        huge = b"\xff\xd8" + b"0" * (10 * 1024 * 1024 + 10)
        assert len(huge) > 10 * 1024 * 1024        # предел кадра — при чтении запроса в память: tests/test_memory_upload.py
        bad = error_of(env, env.routes.run_identify, env.db, b"junk")
        assert bad.code == "photo_not_image" and "Не удалось открыть" in bad.detail

        env.identify_answer = TimeoutError("нет ответа")
        slow = error_of(env, env.routes.run_identify, env.db, jpeg())
        assert (slow.status_code, slow.code) == (504, "identify_timeout")
    finally:
        restore()


def test_a_bad_photo_in_a_form_reports_its_number_and_a_translatable_reason(tmp_path):
    env, restore = with_env(tmp_path)
    try:
        tiny = jpeg(3, 40, 30)
        exc = error_of(env, upload, env, files=[Upload(jpeg(1)), Upload(tiny)])
        assert exc.status_code == 422 and exc.code == "photo_numbered" and exc.detail.startswith("Фото 2:")
        assert exc.params == {"n": 2, "reason": {"key": "server.photo_too_small", "params": {"width": 40, "height": 30}}}
        empty_name = error_of(env, upload, env, name="   ")
        assert empty_name.code == "field_required" and empty_name.params["field"]["key"] == "server.field_name"
    finally:
        restore()
