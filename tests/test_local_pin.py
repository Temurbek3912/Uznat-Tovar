"""Простой вход на этом компьютере (решение владельца 2026-11-15): ПИН-код и ссылка «Придумайте ПИН».

Чистые правила (app/core/local_pin.py) и HTTP: ПИН принимается только с самого компьютера программы; с сети, через туннель
(заголовки прокси), с чужого имени (подмена DNS) и с чужого сайта (Origin) — отказ. Ссылка одноразовая, временный пароль после
неё не действует, 10 ошибок подряд снимают ПИН, сеанс по ПИН закрывается после простоя."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core import local_pin

# ------------------------------------------------------------------ чистые правила


@pytest.mark.parametrize("pin", ["482917", " 482917 ", "905113"])
def test_good_pins(pin):
    assert local_pin.check_pin(pin) == pin.strip()


@pytest.mark.parametrize("pin,code", [("", "pin_format"), ("12345", "pin_format"), ("1234567", "pin_format"),
                                      ("12a456", "pin_format"), ("١٢٣٤٥٦", "pin_format"), ("000000", "pin_too_simple"),
                                      ("123456", "pin_too_simple"), ("654321", "pin_too_simple")])
def test_bad_pins(pin, code):
    with pytest.raises(local_pin.PinPolicyError) as exc:
        local_pin.check_pin(pin)
    assert exc.value.code == code


def _scope(server="127.0.0.1", client="127.0.0.1"):
    return {"server": (server, 8000), "client": (client, 50123)}


def test_this_computer_rules():
    ok = {"host": "localhost:8000"}
    assert local_pin.is_this_computer(_scope(), ok)
    assert local_pin.is_this_computer(_scope("::1", "::1"), {"host": "[::1]:8000"})
    assert local_pin.is_this_computer(_scope(), {"host": "127.0.0.1:8000", "origin": "http://localhost:8000"})
    assert not local_pin.is_this_computer(_scope(client="192.168.1.20"), ok)             # телефон в Wi-Fi
    assert not local_pin.is_this_computer(_scope(server="0.0.0.0"), ok)
    assert not local_pin.is_this_computer(_scope(), {"host": "localhost", "x-forwarded-for": "8.8.8.8"})    # туннель
    assert not local_pin.is_this_computer(_scope(), {"host": "shop.tail046d32.ts.net"})   # имя туннеля / подмена DNS
    assert not local_pin.is_this_computer(_scope(), {"host": "localhost", "origin": "https://evil.example"})
    assert not local_pin.is_this_computer(_scope(), {"host": "localhost", "origin": "null"})
    assert not local_pin.is_this_computer({}, ok)


def test_idle_rule():
    from app.core.security import utcnow
    now = utcnow()
    assert not local_pin.pin_session_idle(now - timedelta(minutes=5), now)
    assert local_pin.pin_session_idle(now - timedelta(minutes=local_pin.PIN_IDLE_MINUTES + 1), now)


# ------------------------------------------------------------------ HTTP

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api.routes_auth import login_throttle  # noqa: E402
from app.api.routes_auth import router as auth_router  # noqa: E402
from app.core.error_handlers import install_error_handlers  # noqa: E402
from app.core.security import Role  # noqa: E402
from app.db.models import AuthSessionMeta, Base, User  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402
from tests.admin_login import admin_entry  # noqa: E402

PASSWORD = "correct-horse-battery"
PIN = "482917"


@pytest.fixture
def env(monkeypatch):
    login_throttle.reset()
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
    install_error_handlers(app)
    app.include_router(auth_router)
    app.dependency_overrides[get_db] = override_get_db
    settings = admin_entry(monkeypatch, app_listener="all")
    with Session() as db:
        service = AuthService(db)
        admin = service.create_user("admin", PASSWORD, Role.ADMIN)
        service.set_temporary_password(admin.id, "temporary-pass-1", hours=None)     # как у владельца: «сменить пароль»
        service.create_user("merch", PASSWORD, Role.MERCHANDISER)

    class E:
        pass

    e = E()
    e.Session, e.settings = Session, settings
    e.local = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50001))
    e.phone = TestClient(app, base_url="http://127.0.0.1:8000", client=("192.168.1.20", 50002))
    yield e
    login_throttle.reset()


def _link(e, username=None):
    with e.Session() as db:
        service = AuthService(db)
        user = service.setup_link_target(username)
        raw, _ = service.create_setup_link(user)
    return raw


def _me(client, token):
    return client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})


def test_setup_link_flow(env):
    info = env.local.get("/api/v1/auth/local").json()
    assert info["this_computer"] is True and info["setup_needed"] is True and info["users"] == []
    raw = _link(env)
    check = env.local.post("/api/v1/auth/setup/check", json={"token": raw}).json()
    assert check == {"valid": True, "username": "admin"}
    assert env.local.post("/api/v1/auth/setup", json={"token": raw, "pin": "111111"}).json()["code"] == "pin_too_simple"
    resp = env.local.post("/api/v1/auth/setup", json={"token": raw, "pin": PIN})
    assert resp.status_code == 200, resp.text
    me = _me(env.local, resp.json()["access_token"])
    assert me.status_code == 200 and "store:manage" in me.json()["permissions"]        # права управления на этом компьютере
    # ссылка одноразовая, временный пароль больше не действует
    again = env.local.post("/api/v1/auth/setup", json={"token": raw, "pin": "905113"})
    assert again.status_code == 401 and again.json()["code"] == "setup_link_invalid"
    assert env.local.post("/api/v1/auth/login", data={"username": "admin", "password": "temporary-pass-1"}).status_code == 401
    info = env.local.get("/api/v1/auth/local").json()
    assert info["setup_needed"] is False and info["users"][0]["username"] == "admin"


def test_pin_login_only_on_this_computer(env):
    raw = _link(env)
    env.local.post("/api/v1/auth/setup", json={"token": raw, "pin": PIN})
    ok = env.local.post("/api/v1/auth/pin", json={"username": "admin", "pin": PIN})
    assert ok.status_code == 200, ok.text
    for client, headers in ((env.phone, {}), (env.local, {"X-Forwarded-For": "8.8.8.8"}),
                            (env.local, {"Origin": "https://evil.example"}), (env.local, {"Host": "evil.example"})):
        resp = client.post("/api/v1/auth/pin", json={"username": "admin", "pin": PIN}, headers=headers)
        assert resp.status_code == 403 and resp.json()["code"] == "pin_local_only", resp.text
    assert env.phone.get("/api/v1/auth/local").json() == {"this_computer": False, "users": [], "setup_needed": False}
    assert env.phone.post("/api/v1/auth/setup/check", json={"token": raw}).status_code == 403


def test_wrong_pins_throttle_then_disable(env):
    env.local.post("/api/v1/auth/setup", json={"token": _link(env), "pin": PIN})
    for _ in range(5):
        assert env.local.post("/api/v1/auth/pin", json={"username": "admin", "pin": "905113"}).status_code == 401
    assert env.local.post("/api/v1/auth/pin", json={"username": "admin", "pin": PIN}).status_code == 429
    login_throttle.reset()
    for _ in range(local_pin.PIN_MAX_FAILURES - 5):
        env.local.post("/api/v1/auth/pin", json={"username": "admin", "pin": "905113"})
        login_throttle.reset()
    # 10 ошибок подряд — ПИН снят: даже верный не подходит, нужна новая ссылка
    assert env.local.post("/api/v1/auth/pin", json={"username": "admin", "pin": PIN}).status_code == 401
    assert env.local.get("/api/v1/auth/local").json()["setup_needed"] is True


def test_pin_session_closes_after_idle(env):
    resp = env.local.post("/api/v1/auth/setup", json={"token": _link(env), "pin": PIN})
    token = resp.json()["access_token"]
    assert _me(env.local, token).status_code == 200
    with env.Session() as db:
        meta = db.query(AuthSessionMeta).one()
        assert meta.method == "pin"
        meta.last_used_at = meta.last_used_at - timedelta(minutes=local_pin.PIN_IDLE_MINUTES + 2)
        db.commit()
    assert _me(env.local, token).status_code == 401


def test_change_pin_and_reauth_with_pin(env):
    token = env.local.post("/api/v1/auth/setup", json={"token": _link(env), "pin": PIN}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    bad = env.local.put("/api/v1/auth/pin", json={"pin": "905113", "current_pin": "000001"}, headers=h)
    assert bad.status_code == 422 and bad.json()["code"] == "pin_wrong"
    assert env.local.put("/api/v1/auth/pin", json={"pin": "905113", "current_pin": PIN}, headers=h).status_code == 200
    assert env.local.post("/api/v1/auth/reauth", json={"pin": "905113"}, headers=h).status_code == 200
    assert env.local.post("/api/v1/auth/reauth", json={"pin": PIN}, headers=h).status_code == 422
    assert env.local.post("/api/v1/auth/pin", json={"username": "admin", "pin": "905113"}).status_code == 200


def test_setup_link_target_prefers_owner_then_admin(env):
    with env.Session() as db:
        service = AuthService(db)
        assert service.setup_link_target().username == "admin"
        assert service.setup_link_target("merch").username == "merch"
        owner = service.create_user("boss", PASSWORD, Role.ADMIN)
        service.set_owner(owner.id)
        assert service.setup_link_target().username == "boss"
        assert db.query(User).count() == 3


def test_set_password_with_pin_for_other_devices(env):
    """После «Придумайте ПИН» пароля человек не знает: на этом компьютере его можно задать, подтвердив ПИН-кодом (2026-11-15)."""
    token = env.local.post("/api/v1/auth/setup", json={"token": _link(env), "pin": PIN}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    url = "/api/v1/auth/password/by-pin"
    bad = env.local.post(url, json={"pin": "905113", "new_password": "my-new-password-1"}, headers=h)
    assert bad.status_code == 422 and bad.json()["code"] == "pin_wrong"
    short = env.local.post(url, json={"pin": PIN, "new_password": "short"}, headers=h)
    assert short.status_code == 422 and short.json()["code"] == "password_too_short"
    # с другого устройства (и через туннель) — нельзя, даже с верным ПИН
    far = env.local.post(url, json={"pin": PIN, "new_password": "my-new-password-1"}, headers={**h, "X-Forwarded-For": "8.8.8.8"})
    assert far.status_code == 403 and far.json()["code"] == "pin_local_only"
    ok = env.local.post(url, json={"pin": PIN, "new_password": "my-new-password-1"}, headers=h)
    assert ok.status_code == 200, ok.text
    assert _me(env.local, token).status_code == 401                         # прежние сеансы завершены
    assert _me(env.local, ok.json()["access_token"]).status_code == 200     # этому устройству — новый
    with env.Session() as db:
        assert {m.method for m in db.query(AuthSessionMeta)} == {"pin"}          # и новый сеанс — тоже по ПИН
    # новым паролем входят с другого устройства (дальше — как обычно: администратору нужен код из приложения)
    resp = env.phone.post("/api/v1/auth/login", data={"username": "admin", "password": "my-new-password-1"})
    assert resp.status_code == 200 and "access_token" not in resp.json(), resp.text
    assert env.local.post("/api/v1/auth/pin", json={"username": "admin", "pin": PIN}).status_code == 200   # ПИН не тронут
