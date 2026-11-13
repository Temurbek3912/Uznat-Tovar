"""
Интеграционные тесты F13 на уровне HTTP: роль × эндпоинт × магазин.

Нужны fastapi, sqlalchemy, httpx (есть в requirements.txt); без них модуль
пропускается целиком. БД — SQLite в памяти, Celery-воркер подменён заглушкой
(настоящий тянет ML-зависимости и Redis, для проверки авторизации не нужен).

ВАЖНО: файл написан в sandbox без FastAPI/SQLAlchemy и там НЕ выполнялся —
только py_compile. Первый реальный `pytest tests/test_auth_api.py -v` —
это его первый прогон; если что-то падает, вероятнее всего это ошибка в самом
тесте, а не в защите (см. docs/HANDOFF.md).
"""
from __future__ import annotations

import importlib
import sys
import types
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.error_handlers import install_error_handlers
from tests.admin_login import admin_entry, admin_token, enable_totp  # noqa: E402
from app.api.deps import get_current_user, oauth2_scheme  # noqa: E402
from app.api.routes_auth import login_throttle  # noqa: E402
from app.api.routes_auth import router as auth_router  # noqa: E402
from app.api.routes_stores import router as stores_router  # noqa: E402
from app.core.security import Role, hash_token, utcnow  # noqa: E402
from app.db.models import ApiToken, Base, Product, Store  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402

PASSWORD = "correct-horse-battery"
PRODUCT_JSON = {
    "sku": "T-1", "name": "Тест", "brand": "B", "category": "C",
    "reference_image_urls": ["http://example.invalid/1.jpg"],
}


class Env:
    """Всё, что нужно тестам: HTTP-клиент, id магазинов/снимков, фабрика сессий, вызовы заглушки воркера."""


def _install_fake_worker(monkeypatch, calls):
    class _Task:
        def __init__(self, bucket):
            self.bucket = bucket

        def delay(self, *args, **kwargs):
            calls[self.bucket].append(args)
            return types.SimpleNamespace(id="task-1")

    class _Celery:
        def AsyncResult(self, task_id):
            return types.SimpleNamespace(ready=lambda: False, status="PENDING", result=None)

    fake = types.ModuleType("app.worker")
    fake.add_catalog_product_task = _Task("add_product")
    # Все задачи, которые импортирует app/api/routes_catalog.py (заглушка отставала от маршрутов — модуль не импортировался).
    fake.add_catalog_photos_task = _Task("add_product")
    fake.identify_product_task = _Task("add_product")
    fake.celery_app = _Celery()
    monkeypatch.setitem(sys.modules, "app.worker", fake)


@pytest.fixture
def env(tmp_path, monkeypatch):
    calls = {"add_product": []}
    _install_fake_worker(monkeypatch, calls)
    login_throttle.reset()  # лимитер живёт в памяти процесса — без сброса неудачи одного теста ломали бы другие

    # Роуты, импортирующие app.worker, должны импортироваться заново — уже с заглушкой.
    fresh = ("app.api.routes_catalog", "app.main")
    for name in fresh:
        sys.modules.pop(name, None)
    routes_catalog = importlib.import_module("app.api.routes_catalog")

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
    for router in (auth_router, stores_router, routes_catalog.router):
        app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db

    e = Env()
    e.auth_settings = admin_entry(monkeypatch)
    e.client = TestClient(app, base_url="http://localhost")
    e.Session = Session
    e.calls = calls
    e.tmp_path = tmp_path

    with Session() as db:
        store_a = Store(name="A", address="-", format="supermarket")
        store_b = Store(name="B", address="-", format="supermarket")
        db.add_all([store_a, store_b])
        db.commit()
        e.store_a, e.store_b = store_a.id, store_b.id

        cola = Product(sku="COLA-1L", name="Кока-кола 1л", brand="Coca-Cola", category="напитки")
        fanta = Product(sku="FANTA-1L", name="Фанта 1л", brand="Coca-Cola", category="напитки")
        db.add_all([cola, fanta])
        db.commit()
        e.cola_id, e.fanta_id = cola.id, fanta.id

        service = AuthService(db)
        service.create_user("admin", PASSWORD, Role.ADMIN)
        service.create_user("merch_a", PASSWORD, Role.MERCHANDISER, [e.store_a])
        service.create_user("region_a", PASSWORD, Role.REGIONAL_MANAGER, [e.store_a])
        service.create_user("analyst", PASSWORD, Role.ANALYST)
    enable_totp(Session, "admin", e.auth_settings)

    yield e

    login_throttle.reset()
    for name in fresh:
        sys.modules.pop(name, None)


def login(e, username, password=PASSWORD):
    return e.client.post("/api/v1/auth/login", data={"username": username, "password": password})


def headers_for(e, username):
    if username == "admin":
        return {"Authorization": f"Bearer {admin_token(e.client, e.Session, username, PASSWORD)}"}
    resp = login(e, username)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def visible_stores(e, headers):
    """Названия магазинов, которые пользователь видит в GET /api/v1/stores: это и есть проверка доступа по магазинам."""
    resp = e.client.get("/api/v1/stores", headers=headers)
    assert resp.status_code == 200, resp.text
    return {s["name"] for s in resp.json()}


# ------------------------------------------------------------------ вход и токены


def test_login_success(env):
    resp = login(env, "merch_a")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == "merchandiser"
    assert body["expires_in"] > 0
    assert len(body["access_token"]) >= 40


def test_login_is_case_insensitive_and_trims_username(env):
    assert login(env, "  MERCH_A ").status_code == 200


def test_wrong_password_and_unknown_user_look_identical(env):
    wrong_pw = login(env, "merch_a", "wrong-password-123")
    no_user = login(env, "nobody", "wrong-password-123")
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()


def test_token_is_stored_only_as_hash(env):
    token = login(env, "merch_a").json()["access_token"]
    with env.Session() as db:
        stored = [row.token_hash for row in db.query(ApiToken).all()]
    assert token not in stored
    assert hash_token(token) in stored


def test_expired_token_is_rejected(env):
    headers = headers_for(env, "merch_a")
    token = headers["Authorization"].split()[1]
    with env.Session() as db:
        row = db.query(ApiToken).filter_by(token_hash=hash_token(token)).one()
        row.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    assert env.client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_garbage_token_is_rejected(env):
    resp = env.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_logout_revokes_token(env):
    headers = headers_for(env, "merch_a")
    assert env.client.get("/api/v1/auth/me", headers=headers).status_code == 200
    assert env.client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert env.client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_me_reports_role_scope_and_permissions(env):
    merch = env.client.get("/api/v1/auth/me", headers=headers_for(env, "merch_a")).json()
    assert merch["role"] == "merchandiser"
    assert merch["all_stores"] is False
    assert merch["store_ids"] == [env.store_a]
    assert "snapshot:upload" in merch["permissions"]
    assert "catalog:write" not in merch["permissions"]

    analyst = env.client.get("/api/v1/auth/me", headers=headers_for(env, "analyst")).json()
    assert analyst["all_stores"] is True
    assert analyst["permissions"] == ["catalog:read", "report:read"]      # матрица прав: ROLE_PERMISSIONS[Role.ANALYST]


# ------------------------------------------------------- «по умолчанию закрыто»


def test_every_route_requires_auth_except_explicitly_public():
    """Страховка от будущей ошибки: новый эндпоинт без авторизации роняет этот тест."""
    sys.modules.pop("app.main", None)
    # заглушка воркера нужна и здесь: app.main импортирует все роутеры
    calls = {"add_product": []}
    mp = pytest.MonkeyPatch()
    try:
        _install_fake_worker(mp, calls)
        for name in ("app.api.routes_catalog",):
            sys.modules.pop(name, None)
        main = importlib.import_module("app.main")
    finally:
        mp.undo()
        for name in ("app.main", "app.api.routes_catalog"):
            sys.modules.pop(name, None)

    # «/» открыт намеренно: адрес сервера без пути (http://192.168.1.50:8000), набранный в браузере телефона, должен открывать главную,
    # а обычный переход по адресу не несёт заголовка Authorization. Что он отдаёт только перенаправление — проверка ниже.
    public = {"/", "/health", "/health/ready", "/api/v1/auth/login"}
    guards = (get_current_user, oauth2_scheme)

    def has_guard(dependant):
        return any(any(dep.call is g for g in guards) or has_guard(dep) for dep in dependant.dependencies)

    unguarded = [
        f"{sorted(r.methods)} {r.path}"
        for r in main.app.routes
        if isinstance(r, APIRoute) and r.path not in public and not has_guard(r.dependant)
    ]
    assert unguarded == []

    root = main.root_redirect()          # «/» ничего не отдаёт, кроме перенаправления на статичную главную страницу
    assert root.status_code in (302, 307) and root.headers.get("location") == "/ui/index.html"


@pytest.mark.parametrize(
    "method,url,kwargs",
    [
        ("get", "/api/v1/auth/me", {}),
        ("get", "/api/v1/stores", {}),
        ("post", "/api/v1/catalog/products", {"json": PRODUCT_JSON}),
        ("get", "/api/v1/catalog/products/tasks/t1", {}),
        ("get", "/api/v1/users", {}),
    ],
)
def test_no_token_means_401(env, method, url, kwargs):
    resp = getattr(env.client, method)(url, **kwargs)
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate", "").lower().startswith("bearer")


# ---------------------------------------------------------------- доступ по магазинам (GET /api/v1/stores)


def test_merchandiser_and_regional_manager_see_only_assigned_stores(env):
    assert visible_stores(env, headers_for(env, "merch_a")) == {"A"}
    assert visible_stores(env, headers_for(env, "region_a")) == {"A"}


def test_admin_and_analyst_see_every_store(env):
    for username in ("admin", "analyst"):
        assert visible_stores(env, headers_for(env, username)) == {"A", "B"}, username


# ----------------------------------------------------------------------- каталог


def test_catalog_is_admin_only(env):
    for username in ("merch_a", "region_a", "analyst"):
        h = headers_for(env, username)
        assert env.client.post("/api/v1/catalog/products", json=PRODUCT_JSON, headers=h).status_code == 403, username
        assert env.client.get("/api/v1/catalog/products/tasks/t1", headers=h).status_code == 403, username

    admin = headers_for(env, "admin")
    assert env.client.post("/api/v1/catalog/products", json=PRODUCT_JSON, headers=admin).status_code == 202
    assert env.client.get("/api/v1/catalog/products/tasks/t1", headers=admin).status_code == 200
    assert len(env.calls["add_product"]) == 1


def test_catalog_search_is_readable_by_every_role(env):
    # В отличие от write — читать/искать каталог может любая роль (нужно мерчандайзеру
    # для "corrected" в F12, остальным — просто посмотреть покрытие каталога).
    for username in ("merch_a", "region_a", "analyst", "admin"):
        resp = env.client.get("/api/v1/catalog/products", headers=headers_for(env, username))
        assert resp.status_code == 200, username
        assert {p["sku"] for p in resp.json()} == {"COLA-1L", "FANTA-1L"}


def test_catalog_search_filters_by_query_across_sku_name_and_brand(env):
    h = headers_for(env, "merch_a")

    by_sku = env.client.get("/api/v1/catalog/products", params={"q": "FANTA"}, headers=h)
    assert [p["sku"] for p in by_sku.json()] == ["FANTA-1L"]

    # Артикулы и бренд у обоих товаров латиницей (COLA-1L / FANTA-1L, Coca-Cola), поэтому кириллица может совпасть только с названием.
    # Прежний запрос «кола» не мог найти «Фанта 1л»: этого слова нет ни в названии, ни в бренде, ни в артикуле.
    by_name = env.client.get("/api/v1/catalog/products", params={"q": "1л"}, headers=h)
    assert {p["sku"] for p in by_name.json()} == {"COLA-1L", "FANTA-1L"}  # «Кока-кола 1л» и «Фанта 1л» оба содержат «1л»

    only_fanta = env.client.get("/api/v1/catalog/products", params={"q": "Фанта"}, headers=h)
    assert [p["sku"] for p in only_fanta.json()] == ["FANTA-1L"]
    only_cola = env.client.get("/api/v1/catalog/products", params={"q": "Кока"}, headers=h)
    assert [p["sku"] for p in only_cola.json()] == ["COLA-1L"]

    by_brand = env.client.get("/api/v1/catalog/products", params={"q": "coca-cola"}, headers=h)  # регистр не важен
    assert {p["sku"] for p in by_brand.json()} == {"COLA-1L", "FANTA-1L"}

    no_match = env.client.get("/api/v1/catalog/products", params={"q": "нет-такого-товара"}, headers=h)
    assert no_match.json() == []


def test_catalog_search_limit_is_clamped(env):
    h = headers_for(env, "admin")
    assert len(env.client.get("/api/v1/catalog/products", params={"limit": 0}, headers=h).json()) <= 2
    assert len(env.client.get("/api/v1/catalog/products", params={"limit": 1}, headers=h).json()) == 1
    resp = env.client.get("/api/v1/catalog/products", params={"limit": 500}, headers=h)
    assert len(resp.json()) == 2   # выше реального количества товаров, но не пытается вернуть "500 пустых"


def test_catalog_search_requires_auth(env):
    resp = env.client.get("/api/v1/catalog/products")
    assert resp.status_code == 401


# ------------------------------------------------------------------ пользователи


def test_only_admin_manages_users(env):
    body = {"username": "newbie", "password": PASSWORD, "role": "analyst"}
    for username in ("merch_a", "region_a", "analyst"):
        h = headers_for(env, username)
        assert env.client.post("/api/v1/users", json=body, headers=h).status_code == 403, username
        assert env.client.get("/api/v1/users", headers=h).status_code == 403, username


def test_admin_creates_user_who_can_then_log_in(env):
    admin = headers_for(env, "admin")
    resp = env.client.post(
        "/api/v1/users", headers=admin,
        json={"username": "Newbie", "password": PASSWORD, "role": "merchandiser", "store_ids": [env.store_a]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["username"] == "newbie"
    assert resp.json()["store_ids"] == [env.store_a]
    # Ни пароль, ни его хеш в ответ не попадают (поля must_change_password / temporary_password — флаги, не пароль).
    assert PASSWORD not in resp.text and "password_hash" not in resp.text and resp.json()["temporary_password"] is None
    # Пароль, заданный администратором, — временный (документ 05, §12.2): вход даёт только пропуск на смену пароля.
    first = login(env, "newbie")
    assert first.status_code == 200 and first.json()["password_change_required"] is True, first.text


def test_create_user_validation_errors(env):
    admin = headers_for(env, "admin")

    def create(**over):
        body = {"username": "someone", "password": PASSWORD, "role": "analyst", **over}
        return env.client.post("/api/v1/users", json=body, headers=admin)

    assert create().status_code == 201
    assert create().status_code == 409                          # дубликат
    assert create(username="other", password="short").status_code == 422   # слабый пароль
    assert create(username="a b").status_code == 422            # плохой логин
    assert create(username="third", store_ids=["no-such-store"]).status_code == 404
    assert create(username="fourth", role="superuser").status_code == 422   # неизвестная роль


def test_admin_cannot_lock_themselves_out(env):
    admin = headers_for(env, "admin")
    me = env.client.get("/api/v1/auth/me", headers=admin).json()
    users = {u["username"]: u["id"] for u in env.client.get("/api/v1/users", headers=admin).json()}
    assert me["username"] == "admin"
    own_id = users["admin"]
    assert env.client.patch(f"/api/v1/users/{own_id}", json={"is_active": False}, headers=admin).status_code == 400
    assert env.client.patch(f"/api/v1/users/{own_id}", json={"role": "analyst"}, headers=admin).status_code == 400
    assert env.client.get("/api/v1/auth/me", headers=admin).status_code == 200


def test_deactivating_user_revokes_existing_tokens_and_blocks_login(env):
    admin = headers_for(env, "admin")
    merch = headers_for(env, "merch_a")
    users = {u["username"]: u["id"] for u in env.client.get("/api/v1/users", headers=admin).json()}

    resp = env.client.patch(f"/api/v1/users/{users['merch_a']}", json={"is_active": False}, headers=admin)
    assert resp.status_code == 200
    assert env.client.get("/api/v1/auth/me", headers=merch).status_code == 401
    assert login(env, "merch_a").status_code == 401


def test_password_change_revokes_old_tokens(env):
    admin = headers_for(env, "admin")
    merch = headers_for(env, "merch_a")
    users = {u["username"]: u["id"] for u in env.client.get("/api/v1/users", headers=admin).json()}

    resp = env.client.patch(f"/api/v1/users/{users['merch_a']}", json={"password": "brand-new-password"}, headers=admin)
    assert resp.status_code == 200
    assert env.client.get("/api/v1/auth/me", headers=merch).status_code == 401
    assert login(env, "merch_a").status_code == 401
    assert login(env, "merch_a", "brand-new-password").status_code == 200


def test_store_assignment_is_replaced_and_takes_effect(env):
    admin = headers_for(env, "admin")
    merch = headers_for(env, "merch_a")
    users = {u["username"]: u["id"] for u in env.client.get("/api/v1/users", headers=admin).json()}
    assert visible_stores(env, merch) == {"A"}

    resp = env.client.patch(f"/api/v1/users/{users['merch_a']}", json={"store_ids": [env.store_b]}, headers=admin)
    assert resp.status_code == 200
    assert resp.json()["store_ids"] == [env.store_b]

    # Токен тот же, но скоуп читается из БД на каждом запросе — новые права действуют сразу.
    assert visible_stores(env, merch) == {"B"}


def test_role_change_takes_effect_immediately(env):
    admin = headers_for(env, "admin")
    merch = headers_for(env, "merch_a")
    users = {u["username"]: u["id"] for u in env.client.get("/api/v1/users", headers=admin).json()}
    before = env.client.get("/api/v1/auth/me", headers=merch).json()
    assert before["role"] == "merchandiser" and "snapshot:upload" in before["permissions"] and visible_stores(env, merch) == {"A"}

    changed = env.client.patch(f"/api/v1/users/{users['merch_a']}", json={"role": "analyst"}, headers=admin)
    assert changed.status_code == 200, changed.text
    # Смена роли завершает все сеансы пользователя (AuthService.update_user, документ 05, §12.11): старый токен больше не действует,
    # новые права — со следующего входа.
    assert env.client.get("/api/v1/auth/me", headers=merch).status_code == 401
    merch = headers_for(env, "merch_a")
    after = env.client.get("/api/v1/auth/me", headers=merch).json()
    assert after["role"] == "analyst" and "snapshot:upload" not in after["permissions"]
    assert visible_stores(env, merch) == {"A", "B"}                             # у аналитика доступ ко всей сети


# ------------------------------------------------- лимит неудачных попыток входа


def test_login_is_throttled_after_repeated_failures(env):
    limit = login_throttle.max_failures_per_user
    for _ in range(limit):
        assert login(env, "merch_a", "wrong-password-1").status_code == 401

    blocked = login(env, "merch_a", "wrong-password-1")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0

    # Во время блокировки отказ и на ВЕРНЫЙ пароль — иначе перебор просто продолжался бы.
    assert login(env, "merch_a").status_code == 429
    # Чужая учётка блокировкой не задета.
    assert login(env, "admin").status_code == 200


def test_unknown_username_is_throttled_like_a_real_one(env):
    # Иначе по моменту появления 429 можно было бы отличить существующий логин от несуществующего.
    for _ in range(login_throttle.max_failures_per_user):
        assert login(env, "ghost", "wrong-password-1").status_code == 401
    assert login(env, "ghost", "wrong-password-1").status_code == 429


def test_successful_login_resets_the_user_counter(env):
    almost = login_throttle.max_failures_per_user - 1
    for _ in range(almost):
        assert login(env, "merch_a", "wrong-password-1").status_code == 401
    assert login(env, "merch_a").status_code == 200
    for _ in range(almost):
        assert login(env, "merch_a", "wrong-password-1").status_code == 401  # снова без 429


def test_ip_limit_blocks_username_spraying(env):
    for i in range(login_throttle.max_failures_per_ip):
        assert login(env, f"spray{i}", "wrong-password-1").status_code == 401
    # Логин свежий, но адрес уже исчерпал лимит; валидная учётка с этого адреса тоже ждёт.
    assert login(env, "fresh-user", "wrong-password-1").status_code == 429
    assert login(env, "admin").status_code == 429


# -------------------------------------- закрытые в сессии F13 ответы 500 -> 404/409


def test_duplicate_username_race_is_409_not_500(env, monkeypatch):
    admin = headers_for(env, "admin")
    body = {"username": "racer", "password": PASSWORD, "role": "analyst"}
    assert env.client.post("/api/v1/users", json=body, headers=admin).status_code == 201

    # Эмулируем гонку: проверка «логин свободен» прошла, а unique-индекс на INSERT уже сработал.
    # Локальный context(): общий monkeypatch.undo() откатил бы заодно и заглушки воркера из фикстуры env.
    with monkeypatch.context() as m:
        m.setattr(AuthService, "_username_taken", lambda self, canon: False)
        assert env.client.post("/api/v1/users", json=body, headers=admin).status_code == 409

    # Сессия после rollback рабочая, дубликата в БД нет.
    users = env.client.get("/api/v1/users", headers=admin)
    assert users.status_code == 200
    assert [u["username"] for u in users.json()].count("racer") == 1


# ------------------------------------------------- язык интерфейса в профиле (миграция 0002)


def test_language_is_kept_in_the_profile_and_follows_the_user_to_a_new_session(env):
    merch = headers_for(env, "merch_a")
    assert env.client.get("/api/v1/auth/me", headers=merch).json()["language"] is None       # не выбран — страница берёт язык браузера
    saved = env.client.put("/api/v1/auth/me/language", headers=merch, json={"language": "uz-Cyrl"})
    assert saved.status_code == 200 and saved.json() == {"language": "uz-Cyrl"}
    other_device = headers_for(env, "merch_a")
    assert env.client.get("/api/v1/auth/me", headers=other_device).json()["language"] == "uz-Cyrl"
    assert env.client.get("/api/v1/auth/me", headers=headers_for(env, "analyst")).json()["language"] is None   # у каждого свой
    cleared = env.client.put("/api/v1/auth/me/language", headers=merch, json={"language": None})
    assert cleared.status_code == 200 and cleared.json() == {"language": None}


def test_unknown_language_is_refused_with_a_code_and_without_token_nothing_is_saved(env):
    merch = headers_for(env, "merch_a")
    bad = env.client.put("/api/v1/auth/me/language", headers=merch, json={"language": "de"})
    assert bad.status_code == 422 and bad.json()["code"] == "language_unknown" and "uz-Cyrl" in bad.json()["params"]["allowed"]
    assert env.client.put("/api/v1/auth/me/language", json={"language": "ru"}).status_code == 401
    assert env.client.get("/api/v1/auth/me", headers=merch).json()["language"] is None
