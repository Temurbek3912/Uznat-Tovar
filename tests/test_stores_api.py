"""
API: GET /api/v1/stores (изоляция магазинов по ролям) и создание магазинов и полок.

ВАЖНО: как и test_auth_api/test_review_api, написан без возможности выполнить (в среде разработки не было
fastapi/sqlalchemy/httpx) — при падении сначала подозревайте сам тест. Чистая логика списка покрыта
исполняемым tests/test_store_service.py.
"""
from __future__ import annotations

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
from app.api.routes_auth import router as auth_router  # noqa: E402
from app.core.security import Role  # noqa: E402
from app.db.models import Base, PlanogramSlotORM, Product, ShelfORM, ShelfSnapshot, Store  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402

PASSWORD = "correct-horse-battery"


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.api.routes_stores import router as stores_router

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
    app.include_router(auth_router)
    app.include_router(stores_router)
    app.dependency_overrides[get_db] = override_get_db

    e = Env()
    e.auth_settings = admin_entry(monkeypatch)
    e.client, e.Session = TestClient(app, base_url="http://localhost"), Session

    with Session() as db:
        a = Store(name="Альфа", address="-", format="supermarket")
        b = Store(name="Бета", address="-", format="supermarket")
        db.add_all([a, b])
        db.commit()
        e.store_a, e.store_b = a.id, b.id
        cola = Product(sku="COLA", name="Кола", brand="X", category="напитки")
        db.add(cola)
        db.commit()
        db.add_all([
            ShelfORM(store_id=e.store_a, shelf_id="a-water", name="Вода"),
            ShelfORM(store_id=e.store_b, shelf_id="b-snacks", name="Снеки"),
            PlanogramSlotORM(shelf_id="a-water", row=0, slot_index=0, expected_product_id=cola.id),
            PlanogramSlotORM(shelf_id="a-water", row=0, slot_index=1, expected_product_id=cola.id),
            # полка, известная только по снимку (демо-данные / снимки до появления реестра)
            ShelfSnapshot(store_id=e.store_a, shelf_id="legacy-a", image_path="/x.jpg", status="done"),
        ])
        db.commit()
        AuthService(db).create_user("admin", PASSWORD, Role.ADMIN)
        AuthService(db).create_user("analyst", PASSWORD, Role.ANALYST)
        AuthService(db).create_user("merch_a", PASSWORD, Role.MERCHANDISER, [e.store_a])
        AuthService(db).create_user("merch_b", PASSWORD, Role.MERCHANDISER, [e.store_b])
        AuthService(db).create_user("nobody", PASSWORD, Role.MERCHANDISER, [])
    enable_totp(Session, "admin", e.auth_settings)

    yield e


def headers_for(e, username):
    if username == "admin":
        return {"Authorization": f"Bearer {admin_token(e.client, e.Session, username, PASSWORD)}"}
    resp = e.client.post("/api/v1/auth/login", data={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_stores_requires_authentication(env):
    assert env.client.get("/api/v1/stores").status_code == 401


def test_merchandiser_sees_only_own_store_with_registered_and_legacy_shelves(env):
    resp = env.client.get("/api/v1/stores", headers=headers_for(env, "merch_a"))
    assert resp.status_code == 200
    body = resp.json()
    assert [s["id"] for s in body] == [env.store_a]               # магазин B не раскрывается
    shelves = {s["shelf_id"]: s for s in body[0]["shelves"]}
    assert set(shelves) == {"a-water", "legacy-a"}
    assert shelves["a-water"]["registered"] is True and shelves["a-water"]["planogram_slots"] == 2
    assert shelves["legacy-a"]["registered"] is False and shelves["legacy-a"]["name"] == "legacy-a"


def test_other_merchandiser_never_sees_foreign_shelves(env):
    body = env.client.get("/api/v1/stores", headers=headers_for(env, "merch_b")).json()
    assert [s["id"] for s in body] == [env.store_b]
    assert [sh["shelf_id"] for sh in body[0]["shelves"]] == ["b-snacks"]


def test_admin_and_analyst_see_all_stores_sorted_by_name(env):
    for user in ("admin", "analyst"):
        body = env.client.get("/api/v1/stores", headers=headers_for(env, user)).json()
        assert [s["name"] for s in body] == ["Альфа", "Бета"]


def test_user_without_assigned_stores_gets_empty_list(env):
    resp = env.client.get("/api/v1/stores", headers=headers_for(env, "nobody"))
    assert resp.status_code == 200 and resp.json() == []


# --------------------------------------------------------------------------- создание магазинов и полок (онбординг админа)

def test_create_store_requires_authentication_and_admin_role(env):
    body = {"name": "Новый", "shelves": [{"name": "Вода"}]}
    assert env.client.post("/api/v1/stores", json=body).status_code == 401
    for user in ("analyst", "merch_a"):                                  # не админ -> 403, магазин не создаётся
        assert env.client.post("/api/v1/stores", json=body, headers=headers_for(env, user)).status_code == 403
    admin = headers_for(env, "admin")
    assert [s["name"] for s in env.client.get("/api/v1/stores", headers=admin).json()] == ["Альфа", "Бета"]


def test_admin_creates_store_with_shelves_and_it_appears_in_the_listing(env):
    admin = headers_for(env, "admin")
    resp = env.client.post("/api/v1/stores", headers=admin, json={
        "name": "Макро Чиланзар", "address": "ул. Бунёдкор, 1", "format": "hypermarket",
        "shelves": [{"name": "Напитки, стеллаж 1"}, {"name": "Снеки", "shelf_id": "chil-snacks-1"}],
    })
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "Макро Чиланзар" and created["format"] == "hypermarket" and created["id"]
    shelves = {s["shelf_id"]: s for s in created["shelves"]}
    assert set(shelves) == {"makro-chilanzar-napitki-stellazh-1", "chil-snacks-1"}
    assert all(s["registered"] is True and s["planogram_slots"] == 0 for s in shelves.values())

    listing = env.client.get("/api/v1/stores", headers=admin).json()
    assert [s["name"] for s in listing] == ["Альфа", "Бета", "Макро Чиланзар"]


def test_store_without_shelves_and_without_address_is_allowed_with_defaults(env):
    resp = env.client.post("/api/v1/stores", headers=headers_for(env, "admin"), json={"name": "Пустой"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["shelves"] == [] and resp.json()["address"] == "" and resp.json()["format"] == "supermarket"


def test_create_store_validation_errors_are_422_and_nothing_is_saved(env):
    admin = headers_for(env, "admin")
    for body in ({"name": "   "}, {"name": "X", "format": "kiosk"},
                 {"name": "X", "shelves": [{"name": "A", "shelf_id": "a-water"}]},          # код занят
                 {"name": "X", "shelves": [{"name": "A", "shelf_id": "bad code"}]},
                 {"name": "X", "shelves": [{"name": "  "}]}):
        resp = env.client.post("/api/v1/stores", headers=admin, json=body)
        assert resp.status_code == 422, (body, resp.text)
    assert [s["name"] for s in env.client.get("/api/v1/stores", headers=admin).json()] == ["Альфа", "Бета"]


def test_admin_adds_hundreds_of_shelves_to_an_existing_store_in_one_request(env):
    admin = headers_for(env, "admin")
    shelves = [{"name": f"Стеллаж {i % 25}"} for i in range(300)]                  # имена повторяются — коды всё равно уникальны
    resp = env.client.post(f"/api/v1/stores/{env.store_a}/shelves", headers=admin, json={"shelves": shelves})
    assert resp.status_code == 201, resp.text
    codes = [s["shelf_id"] for s in resp.json()["shelves"] if s["registered"]]
    assert len(codes) == 301 and len(set(codes)) == 301 and "a-water" in codes     # 300 новых + прежняя «Вода»


def test_add_shelves_to_unknown_store_is_404_and_for_non_admin_403(env):
    admin = headers_for(env, "admin")
    assert env.client.post("/api/v1/stores/nope/shelves", headers=admin, json={"shelves": [{"name": "A"}]}).status_code == 404
    resp = env.client.post(f"/api/v1/stores/{env.store_a}/shelves", headers=headers_for(env, "merch_a"),
                           json={"shelves": [{"name": "A"}]})
    assert resp.status_code == 403


def test_add_shelves_rejects_empty_and_oversized_requests(env):
    admin = headers_for(env, "admin")
    assert env.client.post(f"/api/v1/stores/{env.store_a}/shelves", headers=admin, json={"shelves": []}).status_code == 422
    too_many = {"shelves": [{"name": f"П{i}"} for i in range(501)]}
    assert env.client.post(f"/api/v1/stores/{env.store_a}/shelves", headers=admin, json=too_many).status_code == 422


def test_new_shelf_is_selectable_by_a_merchandiser_assigned_to_that_store(env):
    env.client.post(f"/api/v1/stores/{env.store_a}/shelves", headers=headers_for(env, "admin"),
                    json={"shelves": [{"name": "Соки", "shelf_id": "a-juice"}]})
    body = env.client.get("/api/v1/stores", headers=headers_for(env, "merch_a")).json()
    assert "a-juice" in {s["shelf_id"] for s in body[0]["shelves"]}
