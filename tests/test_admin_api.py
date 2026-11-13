"""
API админ-панели: доступ только администратору, состояние системы, каталог, правка/удаление магазинов и полок.

ВАЖНО: как и остальные API-тесты проекта, написан без возможности выполнить в среде разработки (нет fastapi/sqlalchemy/httpx) —
при падении сначала подозревайте сам тест. Чистая логика покрыта исполняемым test_admin_service.py.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.error_handlers import install_error_handlers
from tests.admin_login import admin_entry, admin_token, enable_totp  # noqa: E402
from app.api import routes_admin, routes_stores  # noqa: E402
from app.api.routes_auth import router as auth_router  # noqa: E402
from app.core.security import Role  # noqa: E402
from app.db.models import (Base, CameraORM, ComplianceReportORM, PlanogramSlotORM, Product, ShelfORM,  # noqa: E402
                           ShelfSnapshot, Store)
from app.db.session import get_db  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402

PASSWORD = "correct-horse-battery"


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = types.SimpleNamespace(
        environment="development", app_timezone="Asia/Tashkent", max_upload_mb=25, auth_token_ttl_minutes=720,
        auth_max_failed_logins_per_user=5, auth_max_failed_logins_per_ip=20, auth_lockout_window_minutes=15,
        media_root=str(tmp_path / "media"), queue_broker_url="redis://:TOPSECRET@r:6379/0",
        database_url="postgresql://u:DBSECRET@h/x", detector=None, matcher=None)
    monkeypatch.setattr(routes_admin, "get_settings", lambda: settings)
    monkeypatch.setattr(routes_admin, "_queue_state", lambda: "ok")
    monkeypatch.setattr(routes_admin, "_worker_state", lambda: {"online": True, "nodes": ["celery@w1"]})

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    install_error_handlers(app)          # как в app/main.py: ошибки в едином виде {detail, code, params}
    for router in (auth_router, routes_admin.router, routes_stores.router):
        app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db

    e = Env()
    e.auth_settings = admin_entry(monkeypatch)
    e.client, e.Session, e.settings = TestClient(app, base_url="http://localhost"), Session, settings
    with Session() as db:
        a, b = Store(name="Альфа", address="ул. 1", format="supermarket"), Store(name="Бета", address="-", format="hypermarket")
        db.add_all([a, b])
        db.commit()
        e.a, e.b = a.id, b.id
        cola, chips = Product(sku="COLA", name="Кола", brand="X", category="напитки"), Product(sku="CHIPS", name="Чипсы", brand="Y", category="снеки")
        db.add_all([cola, chips])
        db.commit()
        db.add_all([ShelfORM(store_id=a.id, shelf_id="a-water", name="Вода"), ShelfORM(store_id=a.id, shelf_id="a-empty", name="Пустая"),
                    ShelfORM(store_id=b.id, shelf_id="b-snacks", name="Снеки")])
        db.add_all([PlanogramSlotORM(shelf_id="a-water", row=0, slot_index=0, expected_product_id=cola.id),
                    PlanogramSlotORM(shelf_id="a-water", row=0, slot_index=1, expected_product_id=cola.id),
                    PlanogramSlotORM(shelf_id="a-water", row=1, slot_index=0, expected_product_id=chips.id)])
        db.commit()
        svc = AuthService(db)
        svc.create_user("admin", PASSWORD, Role.ADMIN)
        svc.create_user("analyst", PASSWORD, Role.ANALYST)
        svc.create_user("merch", PASSWORD, Role.MERCHANDISER, [a.id])
    enable_totp(Session, "admin", e.auth_settings)
    return e


def H(env, user="admin"):
    if user == "admin":
        return {"Authorization": f"Bearer {admin_token(env.client, env.Session, user, PASSWORD)}"}
    token = env.client.post("/api/v1/auth/login", data={"username": user, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def add_snapshot(env, sid, store, shelf, status, age_hours=1, osa=None, comp=None):
    with env.Session() as db:
        db.add(ShelfSnapshot(id=sid, store_id=store, shelf_id=shelf, image_path="/x.jpg", status=status,
                             created_at=datetime.utcnow() - timedelta(hours=age_hours)))
        db.commit()
        if osa is not None:
            db.add(ComplianceReportORM(snapshot_id=sid, compliance_score=comp, osa_score=osa, violations_json="[]", empty_slots_json="[]"))
            db.commit()


# ------------------------------------------------------------------ права

def test_every_admin_endpoint_is_admin_only(env):
    paths = ["/api/v1/admin/system", "/api/v1/admin/products"]
    for path in paths:
        assert env.client.get(path).status_code == 401, path
        for user in ("analyst", "merch"):
            assert env.client.get(path, headers=H(env, user)).status_code == 403, (path, user)
        assert env.client.get(path, headers=H(env)).status_code == 200, path


# ------------------------------------------------------------------ система

def test_system_state_has_no_secrets_and_all_settings_are_read_only(env):
    body = env.client.get("/api/v1/admin/system", headers=H(env)).json()
    text = str(body)
    assert "TOPSECRET" not in text and "DBSECRET" not in text
    assert body["database"] == "ok" and body["queue"] == "ok" and body["worker"]["online"] is True
    assert body["timezone"] == "Asia/Tashkent" and body["server_time_local"] and body["storage"]["usage"]["exists"] is False
    assert body["settings"] and all(s["editable"] is False for s in body["settings"])
    assert "только для чтения" in body["settings_note"]


# ------------------------------------------------------------------ каталог

def test_products_list_search_and_paging(env):
    admin = H(env)
    body = env.client.get("/api/v1/admin/products", headers=admin).json()
    assert body["total"] == 2 and [p["sku"] for p in body["items"]] == ["CHIPS", "COLA"]
    assert all("planogram_slots" not in p for p in body["items"])          # понятия планограммы в каталоге больше нет
    assert [p["sku"] for p in env.client.get("/api/v1/admin/products", params={"q": "COL"}, headers=admin).json()["items"]] == ["COLA"]
    page = env.client.get("/api/v1/admin/products", params={"limit": 1, "offset": 1}, headers=admin).json()
    assert page["total"] == 2 and [p["sku"] for p in page["items"]] == ["COLA"]


# ------------------------------------------------------------------ магазины и полки: правка и защищённое удаление

def test_store_can_be_edited_with_validation(env):
    admin = H(env)
    ok = env.client.patch(f"/api/v1/stores/{env.a}", json={"name": "Альфа-2", "format": "hypermarket"}, headers=admin)
    assert ok.status_code == 200 and ok.json()["name"] == "Альфа-2" and ok.json()["format"] == "hypermarket" and ok.json()["address"] == "ул. 1"
    assert env.client.patch(f"/api/v1/stores/{env.a}", json={"name": "  "}, headers=admin).status_code == 422
    assert env.client.patch(f"/api/v1/stores/{env.a}", json={"format": "kiosk"}, headers=admin).status_code == 422
    assert env.client.patch("/api/v1/stores/nope", json={"name": "x"}, headers=admin).status_code == 404
    assert env.client.patch(f"/api/v1/stores/{env.a}", json={"name": "x"}, headers=H(env, "merch")).status_code == 403


def test_shelf_rename_keeps_the_code(env):
    admin = H(env)
    out = env.client.patch(f"/api/v1/stores/{env.a}/shelves/a-water", json={"name": "Напитки"}, headers=admin).json()
    assert {s["shelf_id"]: s["name"] for s in out["shelves"]}["a-water"] == "Напитки"
    assert env.client.patch(f"/api/v1/stores/{env.a}/shelves/a-water", json={"name": " "}, headers=admin).status_code == 422
    assert env.client.patch(f"/api/v1/stores/{env.b}/shelves/a-water", json={"name": "x"}, headers=admin).status_code == 404   # полка не из этого магазина


def test_shelf_with_snapshots_is_protected_and_empty_shelf_can_be_deleted(env):
    admin = H(env)
    add_snapshot(env, "s1", env.a, "a-water", "done")
    blocked = env.client.delete(f"/api/v1/stores/{env.a}/shelves/a-water", params={"with_planogram": "true"}, headers=admin)
    assert blocked.status_code == 409 and "снимки" in blocked.json()["detail"]
    with env.Session() as db:
        assert db.query(PlanogramSlotORM).filter_by(shelf_id="a-water").count() == 3          # ничего не удалено
    assert env.client.delete(f"/api/v1/stores/{env.a}/shelves/a-empty", headers=admin).status_code == 204


def test_shelf_planogram_is_deleted_only_with_explicit_confirmation(env):
    admin = H(env)
    assert env.client.delete(f"/api/v1/stores/{env.a}/shelves/a-water", headers=admin).status_code == 409
    assert env.client.delete(f"/api/v1/stores/{env.a}/shelves/a-water", params={"with_planogram": "true"}, headers=admin).status_code == 204
    with env.Session() as db:
        assert db.query(PlanogramSlotORM).filter_by(shelf_id="a-water").count() == 0 and db.query(ShelfORM).filter_by(shelf_id="a-water").count() == 0


def test_shelf_with_a_camera_cannot_be_deleted(env):
    with env.Session() as db:
        db.add(CameraORM(name="К", kind="usb", source_enc="x", source_masked="0", store_id=env.a, shelf_id="a-empty"))
        db.commit()
    resp = env.client.delete(f"/api/v1/stores/{env.a}/shelves/a-empty", headers=H(env))
    assert resp.status_code == 409 and "камеры" in resp.json()["detail"]


def test_only_an_empty_store_can_be_deleted(env):
    admin = H(env)
    blocked = env.client.delete(f"/api/v1/stores/{env.b}", headers=admin)
    assert blocked.status_code == 409 and "полки" in blocked.json()["detail"]
    assert env.client.delete(f"/api/v1/stores/{env.b}/shelves/b-snacks", headers=admin).status_code == 204
    assert env.client.delete(f"/api/v1/stores/{env.b}", headers=admin).status_code == 204
    assert env.client.delete(f"/api/v1/stores/{env.b}", headers=admin).status_code == 404
