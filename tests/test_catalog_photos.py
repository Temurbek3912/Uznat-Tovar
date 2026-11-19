"""Каталог, этап 4 (шаг Р-7, первая часть): фото товара из базы (product_images) на странице и в API «Товары», управление фото
(вывести из эталонов, вернуть, сделать витринным) с записью в журнал действий, и учёт этого в распознавании — без пересборки
индекса и без удаления файлов. База — SQLite всегда и PostgreSQL 16, если задан FULL_BACKUP_TEST_DB_URL."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import types
import uuid
from datetime import datetime, timedelta

import numpy as np
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("httpx")
pytest.importorskip("alembic")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.error_handlers import install_error_handlers  # noqa: E402
from app.core.security import Role  # noqa: E402
from app.db import migrate  # noqa: E402
from app.db.models import AuditEvent, Product, ProductImage  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.pipeline import matcher as M  # noqa: E402
from app.services import identify_service  # noqa: E402
from app.services import product_images as P  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402
from app.services.catalog_upload import folder_name, upload_ref  # noqa: E402
from tests.admin_login import admin_entry, admin_token, enable_totp  # noqa: E402

PASSWORD = "correct-horse-battery"


def _jpeg(rgb, size=(300, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, "JPEG", quality=95)
    return buf.getvalue()


@pytest.fixture(params=["sqlite", "postgresql"])
def database(request, tmp_path):
    if request.param == "sqlite":
        yield create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}", connect_args={"check_same_thread": False})
        return
    url = os.environ.get("FULL_BACKUP_TEST_DB_URL")
    if not url:
        pytest.skip("нет FULL_BACKUP_TEST_DB_URL — временного PostgreSQL")
    from app.db.url import normalize_database_url
    admin = create_engine(normalize_database_url(url), isolation_level="AUTOCOMMIT")
    name = f"cph_{uuid.uuid4().hex[:10]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    eng = create_engine(admin.url.set(database=name))
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture
def env(tmp_path, monkeypatch, database):
    migrate.upgrade_to_head(database)
    Session = sessionmaker(bind=database, autoflush=False, autocommit=False)
    e = types.SimpleNamespace(media=tmp_path / "media", Session=Session, engine=database)
    e.uploads = e.media / "catalog-uploads"

    def task(name):
        return types.SimpleNamespace(delay=lambda *a: types.SimpleNamespace(id=name), apply_async=lambda *a, **k: None)
    fake_worker = types.ModuleType("app.worker")
    fake_worker.add_catalog_product_task = task("add")
    fake_worker.add_catalog_photos_task = task("photos")
    fake_worker.identify_product_task = task("identify")
    fake_worker.celery_app = types.SimpleNamespace(AsyncResult=lambda tid: None)
    monkeypatch.setitem(sys.modules, "app.worker", fake_worker)
    sys.modules.pop("app.api.routes_catalog", None)
    import importlib
    routes_catalog = importlib.import_module("app.api.routes_catalog")
    from app.api.routes_auth import router as auth_router
    monkeypatch.setattr(routes_catalog, "get_settings", lambda: types.SimpleNamespace(max_upload_mb=1, media_root=str(e.media)))

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(routes_catalog.router)
    app.dependency_overrides[get_db] = override_get_db
    settings = admin_entry(monkeypatch)
    e.client = TestClient(app, base_url="http://localhost")
    with Session() as db:
        AuthService(db).create_user("admin", PASSWORD, Role.ADMIN)
        AuthService(db).create_user("analyst", PASSWORD, Role.ANALYST)
    enable_totp(Session, "admin", settings)
    e.admin = {"Authorization": f"Bearer {admin_token(e.client, Session, 'admin', PASSWORD)}"}
    resp = e.client.post("/api/v1/auth/login", data={"username": "analyst", "password": PASSWORD})
    e.analyst = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    yield e
    sys.modules.pop("app.api.routes_catalog", None)
    database.dispose()


def _product(e, sku="COLA", name="Кола") -> str:
    with e.Session() as db:
        p = Product(sku=sku, name=name, brand="X", category="Напитки", created_at=datetime(2026, 11, 14))
        db.add(p)
        db.commit()
        return p.id


def _photo_file(e, sku, n, rgb, size=(300, 200)) -> tuple[str, bytes]:
    data = _jpeg(rgb, size)
    name = f"{n:03d}-{hashlib.sha256(data).hexdigest()[:12]}.jpg"
    folder = e.uploads / folder_name(sku)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return upload_ref(folder.name, name), data


def _register(e, pid, sku, n, rgb, role=P.REFERENCE, size=(300, 200)) -> str:
    ref, _data = _photo_file(e, sku, n, rgb, size)
    info = P.upload_photo_info(e.uploads, ref)
    info = P.PhotoInfo(**{**info.__dict__, "role": role})
    with e.Session() as db:
        image_id = P.PhotoRegistry(db).register(pid, info)
    with e.Session() as db:                          # разное время записи — порядок фото определён
        row = db.get(ProductImage, image_id)
        row.created_at = datetime(2026, 11, 14) + timedelta(minutes=n)
        db.commit()
    return image_id


def _snapshot(folder):
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(folder.iterdir()) if not p.name.startswith("thumb-")}


# ------------------------------------------------------------------ список «Товары» из базы


def test_list_takes_photo_count_and_cover_from_the_database(env):
    pid = _product(env)
    first = _register(env, pid, "COLA", 1, (200, 10, 10))
    _register(env, pid, "COLA", 2, (10, 200, 10))
    _photo_file(env, "COLA", 3, (1, 2, 3))                         # файл, которого нет в базе, не считается
    row = env.client.get("/api/v1/catalog/products?q=Кола", headers=env.analyst).json()[0]
    assert row["photo_count"] == 2 and row["reference_count"] == 2 and row["cover_image_id"] == first
    assert set(row) >= {"id", "sku", "name", "brand", "category", "expected_price", "photo_count", "barcodes"}   # прежние поля на месте


def test_product_without_records_keeps_the_old_folder_count(env):
    """Фото ещё не перенесены в базу (воркер не запускался) — как раньше: число файлов в папке, миниатюра — первый файл."""
    pid = _product(env, "P1", "Один")
    _photo_file(env, "P1", 1, (50, 50, 50), (1200, 900))
    _photo_file(env, "P1", 2, (60, 60, 60))
    row = env.client.get("/api/v1/catalog/products?q=Один", headers=env.admin).json()[0]
    assert row["photo_count"] == 2 and row["reference_count"] is None and row["cover_image_id"] is None
    thumb = env.client.get(f"/api/v1/catalog/products/{pid}/photo", headers=env.admin)
    assert thumb.status_code == 200 and max(Image.open(io.BytesIO(thumb.content)).size) == 240
    full = env.client.get(f"/api/v1/catalog/products/{pid}/photo?size=full&n=0", headers=env.admin)
    assert max(Image.open(io.BytesIO(full.content)).size) == 1200


def test_display_photo_is_the_cover_and_each_photo_is_served_by_its_id(env):
    pid = _product(env)
    ref = _register(env, pid, "COLA", 1, (200, 10, 10), size=(800, 600))
    shop = _register(env, pid, "COLA", 2, (10, 10, 200), role=P.DISPLAY, size=(1000, 500))
    row = env.client.get("/api/v1/catalog/products", headers=env.admin).json()[0]
    assert row["cover_image_id"] == shop and row["photo_count"] == 2 and row["reference_count"] == 1
    full = env.client.get(f"/api/v1/catalog/products/{pid}/photo?size=full", headers=env.admin)
    assert Image.open(io.BytesIO(full.content)).size == (1000, 500)                    # витринное
    one = env.client.get(f"/api/v1/catalog/products/{pid}/photo?size=full&image_id={ref}", headers=env.analyst)
    assert Image.open(io.BytesIO(one.content)).size == (800, 600)
    other = _product(env, "PEPSI", "Пепси")
    assert env.client.get(f"/api/v1/catalog/products/{other}/photo?image_id={ref}", headers=env.admin).status_code == 404   # чужое фото
    assert env.client.get(f"/api/v1/catalog/products/{pid}/photo?image_id=nope", headers=env.admin).status_code == 404


def test_photo_record_outside_media_root_is_never_served(env):
    pid = _product(env)
    image_id = _register(env, pid, "COLA", 1, (1, 1, 1))
    secret = env.media.parent / "secret.jpg"
    secret.write_bytes(_jpeg((9, 9, 9)))
    with env.Session() as db:
        db.get(ProductImage, image_id).storage_path = "../secret.jpg"
        db.commit()
    assert env.client.get(f"/api/v1/catalog/products/{pid}/photo?image_id={image_id}", headers=env.admin).status_code == 404


# ------------------------------------------------------------------ управление фото


def test_images_list_shows_roles_without_server_paths(env):
    pid = _product(env)
    a = _register(env, pid, "COLA", 1, (200, 10, 10))
    b = _register(env, pid, "COLA", 2, (10, 10, 200), role=P.DISPLAY)
    body = env.client.get(f"/api/v1/catalog/products/{pid}/images", headers=env.analyst).json()
    assert [i["id"] for i in body["images"]] == [a, b] and body["reference_count"] == 1 and body["cover_image_id"] == b
    assert body["images"][0]["in_recognition"] is True and body["images"][1]["in_recognition"] is False
    assert body["images"][1]["is_cover"] is True and body["images"][0]["has_file"] is True
    raw = json.dumps(body)
    assert "catalog-uploads" not in raw and "storage_path" not in raw and "sha256" not in raw
    assert env.client.get("/api/v1/catalog/products/nope/images", headers=env.admin).status_code == 404


def test_deactivate_and_return_keep_files_and_are_written_to_the_journal(env):
    pid = _product(env)
    a = _register(env, pid, "COLA", 1, (200, 10, 10))
    _register(env, pid, "COLA", 2, (10, 200, 10))
    folder = env.uploads / folder_name("COLA")
    before = _snapshot(folder)
    url = f"/api/v1/catalog/products/{pid}/images/{a}"
    resp = env.client.patch(url, headers=env.admin, json={"active": False, "reason": "чужая упаковка"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] == ["active"] and body["reference_count"] == 1
    changed = next(i for i in body["images"] if i["id"] == a)
    assert changed["active"] is False and changed["in_recognition"] is False and changed["deactivated_at"]
    row = env.client.get("/api/v1/catalog/products", headers=env.admin).json()[0]
    assert row["photo_count"] == 2 and row["reference_count"] == 1                    # фото не исчезло, только не эталон
    assert env.client.patch(url, headers=env.admin, json={"active": True}).status_code == 200
    assert env.client.patch(url, headers=env.admin, json={"active": True}).json()["changed"] == []   # без изменений — без записи
    assert _snapshot(folder) == before                                                  # файлы не тронуты
    with env.Session() as db:
        assert db.query(ProductImage).count() == 2
        events = db.query(AuditEvent).filter(AuditEvent.target_id == pid).order_by(AuditEvent.at, AuditEvent.id).all()
        assert [e.action for e in events] == ["catalog.photo.deactivated", "catalog.photo.activated"]
        admin_id = db.execute(text("SELECT id FROM users WHERE username = 'admin'")).scalar_one()
        assert all(e.actor_user_id == admin_id and e.target_type == "product" for e in events)
        assert json.loads(events[0].before_json) == {"image_id": a, "active": True}
        assert json.loads(events[0].after_json) == {"image_id": a, "active": False}
        assert events[0].reason == "чужая упаковка" and events[1].reason is None


def test_make_display_and_back_to_reference(env):
    pid = _product(env)
    a = _register(env, pid, "COLA", 1, (200, 10, 10))
    b = _register(env, pid, "COLA", 2, (10, 200, 10))
    url = f"/api/v1/catalog/products/{pid}/images/{b}"
    body = env.client.patch(url, headers=env.admin, json={"role": "display"}).json()
    assert body["cover_image_id"] == b and body["reference_count"] == 1
    body = env.client.patch(url, headers=env.admin, json={"role": "reference", "active": True}).json()
    assert body["cover_image_id"] == a and body["reference_count"] == 2 and body["changed"] == ["role"]
    with env.Session() as db:
        actions = [e.action for e in db.query(AuditEvent).filter(AuditEvent.target_id == pid).order_by(AuditEvent.at, AuditEvent.id)]
    assert actions == ["catalog.photo.made_display", "catalog.photo.made_reference"]


def test_photo_changes_need_catalog_write_and_a_photo_of_this_product(env):
    pid = _product(env)
    a = _register(env, pid, "COLA", 1, (200, 10, 10))
    other = _product(env, "PEPSI", "Пепси")
    url = f"/api/v1/catalog/products/{pid}/images/{a}"
    assert env.client.patch(url, json={"active": False}).status_code == 401
    assert env.client.patch(url, headers=env.analyst, json={"active": False}).status_code == 403
    assert env.client.patch(f"/api/v1/catalog/products/{other}/images/{a}", headers=env.admin, json={"active": False}).status_code == 404
    assert env.client.patch(f"/api/v1/catalog/products/{pid}/images/nope", headers=env.admin, json={"active": False}).status_code == 404
    bad = env.client.patch(url, headers=env.admin, json={"role": "main"})
    assert bad.status_code == 422 and bad.json()["code"] == "photo_role_invalid"
    with env.Session() as db:
        assert db.get(ProductImage, a).is_active is True
        assert db.query(AuditEvent).filter(AuditEvent.target_id == pid).count() == 0


# ------------------------------------------------------------------ распознавание учитывает выведенные фото


class _Lookup:
    def __init__(self, names):
        self.names = names

    def __call__(self, ids):
        return {i: identify_service.ProductInfo(id=i, name=self.names[i], brand="-", expected_price=None) for i in ids if i in self.names}


class _Embedder:
    def __init__(self, vector):
        self.vector = vector

    def embed(self, images):
        return np.atleast_2d(self.vector)


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_excluded_sources_are_skipped_and_the_search_goes_deeper():
    index = M.NumpyCatalogIndex()
    index.add("cola", np.stack([_unit([1, 0, 0]), _unit([0.9, 0.1, 0])]), ["upload:c/1", "upload:c/2"])
    index.add("pepsi", _unit([0.7, 0.7, 0]), ["upload:p/1"])
    matcher = M.ProductMatcher(index, top_k=1, candidates_k=1)
    query = _unit([1, 0, 0])
    assert matcher.rank(query)[0][0] == "cola"
    matcher.excluded = frozenset({("cola", "upload:c/1"), ("cola", "upload:c/2")})
    assert [pid for pid, _ in matcher.rank(query)] == ["pepsi"]             # k=1 участвующий вектор, а не «пусто»
    matcher.excluded = frozenset({("cola", "upload:c/1")})
    ranked = dict(matcher.rank(query, k=3))
    assert ranked["cola"] == pytest.approx(float(_unit([0.9, 0.1, 0]) @ query))   # остался второй эталон
    matcher.excluded = frozenset({("pepsi", "upload:c/1")})                 # тот же источник у другого товара не мешает
    assert matcher.rank(query)[0] == ("cola", pytest.approx(1.0))


def test_matcher_over_index_without_sources_search_works_as_before():
    class Old:
        def search(self, query, top_k):
            return [("a", 0.9)]
    matcher = M.ProductMatcher(Old())
    matcher.excluded = frozenset({("a", "x")})
    assert matcher.rank(np.zeros(3)) == [("a", 0.9)]


def test_deactivated_photo_is_not_recognized_and_returning_it_brings_it_back(env, monkeypatch):
    """Сквозной путь без пересборки индекса: фото выведено на странице -> запись в базе -> список исключений воркера ->
    распознавание этого фото не находит; вернули — снова находит."""
    cola, pepsi = _product(env), _product(env, "PEPSI", "Пепси")
    a = _register(env, cola, "COLA", 1, (200, 10, 10))
    b = _register(env, pepsi, "PEPSI", 1, (10, 200, 10))
    with env.Session() as db:
        src = {r.id: r.source_ref for r in db.query(ProductImage).all()}
    index = M.NumpyCatalogIndex()
    index.add(cola, _unit([1, 0, 0]), [src[a]])
    index.add(pepsi, _unit([0.5, 0.86, 0]), [src[b]])
    matcher = M.ProductMatcher(index, match_threshold=0.9, unknown_threshold=0.3)
    lookup, frame = _Lookup({cola: "Кола", pepsi: "Пепси"}), np.zeros((4, 4, 3), dtype=np.uint8)
    embedder = _Embedder(_unit([1, 0, 0]))

    fake = sys.modules.pop("app.worker")                     # настоящая функция воркера (в env модуль задач подменён)
    try:
        import importlib
        worker = importlib.import_module("app.worker")
    finally:
        sys.modules["app.worker"] = fake
    monkeypatch.setattr(worker, "_matcher", matcher)

    def recognize():
        with env.Session() as db:
            worker._refresh_exclusions(db)
        return identify_service.identify(frame, embedder, matcher, lookup)

    assert recognize()["product"]["id"] == cola
    env.client.patch(f"/api/v1/catalog/products/{cola}/images/{a}", headers=env.admin, json={"active": False})
    answer = recognize()
    assert answer["product"] is None or answer["product"]["id"] != cola
    assert all(c["id"] != cola for c in answer["candidates"])
    assert index_size_unchanged(index, 2)                       # индекс не пересобирался, вектор на месте
    env.client.patch(f"/api/v1/catalog/products/{cola}/images/{a}", headers=env.admin, json={"active": True})
    assert recognize()["product"]["id"] == cola
    env.client.patch(f"/api/v1/catalog/products/{cola}/images/{a}", headers=env.admin, json={"role": "display"})
    assert all(c["id"] != cola for c in recognize()["candidates"])          # витринное в распознавании не участвует


def index_size_unchanged(index, n) -> bool:
    return M.index_size(index) == n


def test_rebuild_from_cache_also_leaves_deactivated_photos_out(env):
    """Пересборка индекса из кэша (scripts/rebuild_index.py) согласована с исключениями: выведенное фото в новый индекс не входит."""
    pid = _product(env)
    a = _register(env, pid, "COLA", 1, (200, 10, 10))
    b = _register(env, pid, "COLA", 2, (10, 200, 10))
    sig = {"name": "t", "weights": "w", "preprocess": "p", "dim": 3, "normalization": "l2"}
    with env.Session() as db:
        reg = P.PhotoRegistry(db)
        reg.store_vectors([(a, _unit([1, 0, 0])), (b, _unit([0, 1, 0]))], sig)
        reg.set_active(a, False)
        plan = P.plan_rebuild(reg, [pid], reg.model_versions()[0])
    assert [i.image_id for i in plan.items] == [b] and plan.inactive == 1


def test_all_references_removed_means_not_found_rather_than_empty_catalog():
    index = M.NumpyCatalogIndex()
    index.add("cola", _unit([1, 0, 0]), ["upload:c/1"])
    matcher = M.ProductMatcher(index)
    matcher.excluded = frozenset({("cola", "upload:c/1")})
    answer = identify_service.identify(np.zeros((2, 2, 3)), _Embedder(_unit([1, 0, 0])), matcher, _Lookup({"cola": "Кола"}))
    assert answer["status"] == "unknown" and answer["product"] is None
    empty = M.ProductMatcher(M.NumpyCatalogIndex())
    assert identify_service.identify(np.zeros((2, 2, 3)), _Embedder(_unit([1, 0, 0])), empty, _Lookup({}))["status"] == "empty_catalog"


# ------------------------------------------------------------------ товар в архиве и версия правки (миграция 0006)


def test_archive_hides_the_product_from_the_list_writes_the_journal_and_restore_brings_it_back(env):
    pid = _product(env)
    _register(env, pid, "COLA", 1, (200, 10, 10))
    row = env.client.get("/api/v1/catalog/products", headers=env.admin).json()[0]
    assert row["status"] == "active" and row["version"] == 1
    url = f"/api/v1/catalog/products/{pid}/status"
    resp = env.client.post(url, headers=env.admin, json={"status": "archived", "reason": "больше не продаём", "expected_version": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "archived" and resp.json()["version"] == 2
    assert env.client.get("/api/v1/catalog/products", headers=env.admin).json() == []                   # в каталоге нет
    archived = env.client.get("/api/v1/catalog/products?status=archived", headers=env.admin).json()
    assert [p["id"] for p in archived] == [pid] and archived[0]["photo_count"] == 1                    # фото на месте
    assert len(env.client.get("/api/v1/catalog/products?status=all", headers=env.admin).json()) == 1
    assert env.client.post(url, headers=env.admin, json={"status": "archived"}).json()["version"] == 2   # повтор — без изменений
    back = env.client.post(url, headers=env.admin, json={"status": "active"})
    assert back.status_code == 200 and back.json()["status"] == "active" and back.json()["version"] == 3
    with env.Session() as db:
        events = db.query(AuditEvent).filter(AuditEvent.target_id == pid).order_by(AuditEvent.at, AuditEvent.id).all()
        assert [e.action for e in events] == ["catalog.product.archived", "catalog.product.restored"]
        assert json.loads(events[0].before_json) == {"status": "active"} and events[0].reason == "больше не продаём"
        assert db.get(Product, pid).updated_at is not None


def test_status_change_needs_catalog_write_a_valid_state_and_the_current_version(env):
    pid = _product(env)
    url = f"/api/v1/catalog/products/{pid}/status"
    assert env.client.post(url, headers=env.analyst, json={"status": "archived"}).status_code == 403
    bad = env.client.post(url, headers=env.admin, json={"status": "deleted"})
    assert bad.status_code == 422 and bad.json()["code"] == "product_status_invalid"
    assert env.client.get("/api/v1/catalog/products?status=deleted", headers=env.admin).status_code == 422
    assert env.client.post("/api/v1/catalog/products/nope/status", headers=env.admin, json={"status": "archived"}).status_code == 404
    stale = env.client.post(url, headers=env.admin, json={"status": "archived", "expected_version": 7})
    assert stale.status_code == 409 and stale.json()["code"] == "concurrent_update"
    with env.Session() as db:
        assert db.get(Product, pid).status == "active"


def test_edit_bumps_the_version_and_a_stale_edit_is_refused_instead_of_overwriting(env):
    pid = _product(env)
    url = f"/api/v1/catalog/products/{pid}"
    first = env.client.patch(url, headers=env.admin, json={"name": "Кола 0,5 л", "expected_version": 1})
    assert first.status_code == 200 and first.json()["version"] == 2
    stale = env.client.patch(url, headers=env.admin, json={"name": "Кола 1 л", "expected_version": 1})   # вторая вкладка
    assert stale.status_code == 409 and stale.json()["code"] == "concurrent_update"
    assert env.client.patch(url, headers=env.admin, json={"name": "Кола 0,5 л"}).json()["version"] == 2    # без изменений
    assert env.client.patch(url, headers=env.admin, json={"brand": "Y"}).json()["version"] == 3           # без версии — как раньше
    with env.Session() as db:
        assert db.get(Product, pid).name == "Кола 0,5 л"


def test_archived_products_are_skipped_by_the_matcher_with_and_without_sources():
    index = M.NumpyCatalogIndex()
    index.add("cola", np.stack([_unit([1, 0, 0]), _unit([0.9, 0.1, 0])]), ["upload:c/1", "upload:c/2"])
    index.add("pepsi", _unit([0.7, 0.7, 0]), ["upload:p/1"])
    matcher = M.ProductMatcher(index, top_k=1, candidates_k=1)
    query = _unit([1, 0, 0])
    matcher.excluded_products = frozenset({"cola"})
    assert [pid for pid, _ in matcher.rank(query)] == ["pepsi"]

    class Old:
        def search(self, query, top_k):
            return [("cola", 0.95), ("pepsi", 0.6)][:top_k]
    old = M.ProductMatcher(Old(), top_k=1, candidates_k=1)
    old.excluded_products = frozenset({"cola"})
    assert old.rank(np.zeros(3)) == [("pepsi", 0.6)]


def test_shopper_never_sees_an_archived_product(env):
    from app.api.routes_public import public_card, public_result
    pid = _product(env)
    with env.Session() as db:
        p = db.get(Product, pid)
        assert public_card(p)["name"] == "Кола"
        p.status = "archived"
        db.commit()
        p = db.get(Product, pid)
        assert public_card(p) is None
        found = {"status": "matched", "product": {"id": pid}, "candidates": [{"id": pid}]}
        assert public_result(db, found)["status"] == "unknown"


def test_deactivated_photo_is_never_the_cover(env):
    """Выведенное из эталонов фото не становится миниатюрой: первое — выведено, миниатюра — второе; выведены все — миниатюры
    нет (cover_image_id None, фото 404 → на странице заглушка), хотя записи о фото и файлы остаются."""
    pid = _product(env)
    a = _register(env, pid, "COLA", 1, (200, 10, 10), size=(800, 600))
    b = _register(env, pid, "COLA", 2, (10, 200, 10), size=(500, 900))
    url = f"/api/v1/catalog/products/{pid}/images"
    assert env.client.patch(f"{url}/{a}", headers=env.admin, json={"active": False}).json()["cover_image_id"] == b
    full = env.client.get(f"/api/v1/catalog/products/{pid}/photo?size=full", headers=env.admin)
    assert Image.open(io.BytesIO(full.content)).size == (500, 900)
    body = env.client.patch(f"{url}/{b}", headers=env.admin, json={"active": False}).json()
    assert body["cover_image_id"] is None and not any(i["is_cover"] for i in body["images"])
    row = env.client.get("/api/v1/catalog/products", headers=env.admin).json()[0]
    assert row["cover_image_id"] is None and row["photo_count"] == 2
    assert env.client.get(f"/api/v1/catalog/products/{pid}/photo", headers=env.admin).status_code == 404
    assert env.client.get(f"/api/v1/catalog/products/{pid}/photo?image_id={a}", headers=env.admin).status_code == 200   # по номеру — видно
