"""
Вход, сеансы и второй фактор по решениям владельца 2026-11-08 (документ 18) на уровне HTTP: FastAPI + SQLite в памяти.

Нужны fastapi, sqlalchemy, httpx (есть в requirements.txt); без них модуль пропускается целиком.
ВАЖНО: написан в облачной среде без FastAPI и SQLAlchemy и там НЕ выполнялся. Первый настоящий прогон —
`pytest tests/test_auth_sessions_api.py -v` в среде по requirements.txt; падение там вероятнее означает ошибку нового кода или самого
теста (сервис тоже ни разу не работал с настоящей базой — docs/ENGINEERING_STATUS.md, §6.11). Чистая логика проверена отдельно
(test_totp, test_mfa_policy, test_session_info, test_auth_secrets, test_recovery_codes), инварианты маршрутов — test_auth_invariants.

Часы TOTP подменяются (env.now): коды разных шагов можно получать без ожидания, повтор кода проверяется детерминированно.
Вход программы: по умолчанию административный (APP_LISTENER=admin, адрес http://localhost — разрешённое имя); тесты аудита
(документ 18, §12) меняют вход, имя и адрес сервера (TestClient берёт scope["server"] из base_url).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.api.deps as deps  # noqa: E402
import app.api.routes_auth as routes_auth  # noqa: E402
from app.core.error_handlers import install_error_handlers
from app.core import totp  # noqa: E402
from app.core.crypto import generate_key  # noqa: E402
from app.core.security_headers import SecurityHeadersMiddleware  # noqa: E402
from app.core.security import Role, utcnow  # noqa: E402
from app.db.models import AuthSessionMeta, Base, UserSecurity  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402

PASSWORD = "correct-horse-battery"
PHONE_UA = "Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36"
PC_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


class Env:
    pass


@pytest.fixture
def env(monkeypatch):
    routes_auth.login_throttle.reset()
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
    app.include_router(routes_auth.router)
    app.add_middleware(SecurityHeadersMiddleware)
    app.dependency_overrides[get_db] = override_get_db

    e = Env()
    e.settings = routes_auth.get_settings().model_copy(update={
        "auth_secret_key": generate_key(), "auth_require_2fa_for_admins": True, "auth_admin_session_max_hours": 8,
        "auth_token_ttl_minutes": 720, "auth_reauth_minutes": 10, "app_listener": "admin",
        "vpn_bind_ip": "100.64.0.5", "admin_allowed_hosts": ""})
    monkeypatch.setattr(routes_auth, "get_settings", lambda: e.settings)
    monkeypatch.setattr(deps, "get_settings", lambda: e.settings)
    e.now = 1_800_000_000.0
    monkeypatch.setattr(totp.time, "time", lambda: e.now)          # часы второго фактора — под управлением теста
    e.app = app
    e.client, e.Session, e.secrets = TestClient(app, base_url="http://localhost"), Session, {}
    with Session() as db:
        service = AuthService(db)
        e.owner_id = service.create_user("owner", PASSWORD, Role.ADMIN).id
        service.set_owner(e.owner_id)
        e.merch_id = service.create_user("merch", PASSWORD, Role.MERCHANDISER).id
    yield e
    routes_auth.login_throttle.reset()


def login(e, username, password=PASSWORD, ua=PC_UA):
    return e.client.post("/api/v1/auth/login", data={"username": username, "password": password}, headers={"User-Agent": ua})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def settle(e, user_id, *, username=None):
    """Пользователь, созданный через API, начинает с временного пароля (документ 05, §12.2). Для тестов, где важно другое,
    отмечаем, что первый вход уже пройден (пароль тот же). Возвращает id."""
    with e.Session() as db:
        if user_id is None:
            user_id = next(u.id for u in AuthService(db).list_users() if u.username == username)
        sec = db.get(UserSecurity, user_id)
        if sec is not None:
            sec.must_change_password, sec.temp_password_expires_at = False, None
            db.commit()
    return user_id


def code(e, username):
    e.now += 30                                     # следующий шаг: каждый код свежий, повтор проверяется отдельно
    return totp.code_at(e.secrets[username], e.now)


def enroll(e, username, password=PASSWORD):
    """Первый вход администратора: пропуск настройки -> ключ -> код -> обычный сеанс и коды восстановления."""
    first = login(e, username, password=password)
    assert first.status_code == 200 and first.json()["mfa_enrollment_required"] is True, first.text
    enroll_token = first.json()["enroll_token"]
    setup = e.client.post("/api/v1/auth/2fa/setup", json={}, headers=bearer(enroll_token))
    assert setup.status_code == 200, setup.text
    e.secrets[username] = setup.json()["secret"]
    done = e.client.post("/api/v1/auth/2fa/confirm", json={"code": code(e, username)}, headers=bearer(enroll_token))
    assert done.status_code == 200 and done.json()["access_token"] and len(done.json()["recovery_codes"]) == 10, done.text
    return done.json()["access_token"], done.json()["recovery_codes"], enroll_token


def admin_token(e, username="owner", password=PASSWORD):
    if username not in e.secrets:
        return enroll(e, username, password)[0]
    step1 = login(e, username, password=password)
    assert step1.json().get("mfa_required") is True, step1.text
    ok = e.client.post("/api/v1/auth/login/2fa", json={"mfa_token": step1.json()["mfa_token"], "code": code(e, username)})
    assert ok.status_code == 200, ok.text
    return ok.json()["access_token"]


def merch_token(e, ua=PC_UA):
    resp = login(e, "merch", ua=ua)
    assert resp.status_code == 200 and "access_token" in resp.json(), resp.text
    return resp.json()["access_token"]


# ------------------------------------------------------------------ одновременные сеансы и отзыв


def test_concurrent_sessions_are_listed_without_ip_and_ended_independently(env):
    pc, phone, third = merch_token(env), merch_token(env, ua=PHONE_UA), merch_token(env)
    sessions = env.client.get("/api/v1/auth/sessions", headers=bearer(pc)).json()
    assert len(sessions) == 3 and sum(s["current"] for s in sessions) == 1
    assert {s["device_kind"] for s in sessions} == {"computer", "phone"}
    assert "ip" not in {k for s in sessions for k in s} and pc not in str(sessions)
    phone_id = [s["id"] for s in env.client.get("/api/v1/auth/sessions", headers=bearer(phone)).json() if s["current"]][0]
    assert env.client.delete(f"/api/v1/auth/sessions/{phone_id}", headers=bearer(pc)).status_code == 200
    assert env.client.get("/api/v1/auth/me", headers=bearer(phone)).status_code == 401
    assert env.client.get("/api/v1/auth/me", headers=bearer(third)).status_code == 200            # остальные живы
    assert env.client.post("/api/v1/auth/sessions/revoke-others", headers=bearer(pc)).json()["count"] == 1
    assert env.client.get("/api/v1/auth/me", headers=bearer(third)).status_code == 401
    assert env.client.post("/api/v1/auth/logout-all", headers=bearer(pc)).status_code == 200
    assert env.client.get("/api/v1/auth/me", headers=bearer(pc)).status_code == 401


def test_foreign_session_id_is_not_found(env):
    merch = merch_token(env)
    owner_session = env.client.get("/api/v1/auth/sessions", headers=bearer(admin_token(env))).json()[0]["id"]
    assert env.client.delete(f"/api/v1/auth/sessions/{owner_session}", headers=bearer(merch)).status_code == 404


@pytest.mark.parametrize("change", [{"password": "another-strong-pass"}, {"is_active": False}, {"role": "analyst"}])
def test_password_change_disable_and_role_change_end_sessions_in_one_transaction(env, change):
    merch = merch_token(env)
    owner = admin_token(env)
    resp = env.client.patch(f"/api/v1/users/{env.merch_id}", json=change, headers=bearer(owner))
    assert resp.status_code == 200, resp.text
    assert env.client.get("/api/v1/auth/me", headers=bearer(merch)).status_code == 401
    with env.Session() as db:
        assert db.get(UserSecurity, env.merch_id).session_epoch == 1


# ------------------------------------------------------------------ второй фактор обязателен администраторам (В-2, В-3)


def test_admin_first_sign_in_is_an_enrollment_pass_that_opens_nothing_else(env):
    first = login(env, "owner")
    assert "access_token" not in first.json()
    enroll_token = first.json()["enroll_token"]
    assert env.client.get("/api/v1/auth/me", headers=bearer(enroll_token)).status_code == 401           # не сеанс
    assert env.client.get("/api/v1/users", headers=bearer(enroll_token)).status_code == 401
    assert env.client.post("/api/v1/auth/refresh", headers=bearer(enroll_token)).status_code == 401
    assert env.client.get("/api/v1/auth/2fa", headers=bearer(enroll_token)).json()["enrolling"] is True
    token, codes, used_pass = enroll(env, "owner")
    me = env.client.get("/api/v1/auth/me", headers=bearer(token)).json()
    assert me["mfa_enabled"] is True and me["is_owner"] is True
    assert env.client.get("/api/v1/auth/2fa", headers=bearer(used_pass)).status_code == 401           # пропуск израсходован
    with env.Session() as db:
        security = db.get(UserSecurity, env.owner_id)
        assert env.secrets["owner"] not in security.totp_secret_enc
    assert all(len(c) == 11 for c in codes)


def test_totp_replay_and_recovery_codes(env):
    _token, codes, _ = enroll(env, "owner")
    step1 = login(env, "owner")
    challenge = step1.json()["mfa_token"]
    used = totp.code_at(env.secrets["owner"], env.now)                    # код подтверждения — уже использован
    assert env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": challenge, "code": used}).status_code == 422
    ok = env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": challenge, "code": code(env, "owner")})
    assert ok.status_code == 200
    replay_challenge = login(env, "owner").json()["mfa_token"]
    replay = totp.code_at(env.secrets["owner"], env.now)                  # тот же код ещё раз — отказ
    assert env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": replay_challenge, "code": replay}).status_code == 422
    # код восстановления: один раз
    by_recovery = env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": replay_challenge, "code": codes[0].lower()})
    assert by_recovery.status_code == 200 and by_recovery.json()["recovery_left"] == 9
    again = env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": login(env, "owner").json()["mfa_token"], "code": codes[0]})
    assert again.status_code == 422


def test_admin_token_without_second_factor_is_refused_whatever_path_issued_it(env):
    with env.Session() as db:
        service = AuthService(db)
        raw, _ = service.issue_token(service.get_user(env.owner_id), 60, mfa=False)        # «старый» токен или обходной путь
    assert env.client.get("/api/v1/auth/me", headers=bearer(raw)).status_code == 401


def test_admins_cannot_turn_two_factor_off(env):
    token = admin_token(env)
    resp = env.client.post("/api/v1/auth/2fa/disable", json={"password": PASSWORD, "code": code(env, "owner")}, headers=bearer(token))
    assert resp.status_code == 409 and resp.json()["code"] == "mfa_required_for_admins"


def test_attempts_are_limited_per_challenge(env):
    enroll(env, "owner")
    challenge = login(env, "owner").json()["mfa_token"]
    for _ in range(env.settings.auth_mfa_max_attempts - 1):
        assert env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": challenge, "code": "000000"}).status_code == 422
    last = env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": challenge, "code": "000000"})
    assert last.status_code == 401 and last.json()["code"] == "mfa_attempts_exhausted"


def test_without_server_key_enrollment_is_unavailable(env):
    env.settings = env.settings.model_copy(update={"auth_secret_key": ""})
    enroll_token = login(env, "owner").json()["enroll_token"]
    resp = env.client.post("/api/v1/auth/2fa/setup", json={}, headers=bearer(enroll_token))
    assert resp.status_code == 503 and resp.json()["code"] == "mfa_unavailable"


# ------------------------------------------------------------------ административный вход (В-1)


def test_on_the_employee_listener_admin_rights_do_not_apply_and_enrollment_is_refused(env):
    token = admin_token(env)                                    # сеанс открыт на административном входе
    env.settings = env.settings.model_copy(update={"app_listener": "public"})
    assert env.client.get("/api/v1/auth/me", headers=bearer(token)).json()["admin_listener"] is False
    denied = env.client.get("/api/v1/users", headers=bearer(token))
    assert denied.status_code == 403 and denied.json()["code"] == "admin_listener_required"
    with env.Session() as db:
        AuthService(db).create_user("second", PASSWORD, Role.ADMIN)
    refused = login(env, "second")
    assert refused.status_code == 403 and refused.json()["code"] == "mfa_enrollment_required"
    assert "access_token" in login(env, "merch").json()         # сотрудники входят как раньше


# ------------------------------------------------------------------ срок 8 часов, продление, подтверждение (В-4)


def test_admin_session_lasts_at_most_8_hours_and_refresh_rotates_without_moving_the_limit(env):
    login_two = admin_token(env)
    first = env.client.get("/api/v1/auth/sessions", headers=bearer(login_two)).json()
    current = [s for s in first if s["current"]][0]
    assert (current["expires_at"] and
            abs((current_dt(current["expires_at"]) - current_dt(current["created_at"])) - timedelta(hours=8)) < timedelta(minutes=1))
    renewed = env.client.post("/api/v1/auth/refresh", headers=bearer(login_two))
    assert renewed.status_code == 200
    new_token = renewed.json()["access_token"]
    assert env.client.get("/api/v1/auth/me", headers=bearer(login_two)).status_code == 401            # старый токен погашен
    with env.Session() as db:
        for meta in db.query(AuthSessionMeta).filter(AuthSessionMeta.user_id == env.owner_id):
            meta.auth_at = utcnow() - timedelta(hours=8)
        db.commit()
    expired = env.client.post("/api/v1/auth/refresh", headers=bearer(new_token))
    assert expired.status_code == 401 and expired.json()["code"] == "session_max_age"


def current_dt(value):
    from datetime import datetime
    return datetime.fromisoformat(value)


def test_sensitive_operations_need_recent_password_and_code(env):
    token = admin_token(env)
    with env.Session() as db:
        for meta in db.query(AuthSessionMeta).filter(AuthSessionMeta.user_id == env.owner_id):
            meta.reauth_at = utcnow() - timedelta(minutes=30)
        db.commit()
    need = env.client.patch(f"/api/v1/users/{env.merch_id}", json={"is_active": False}, headers=bearer(token))
    assert need.status_code == 403 and need.json()["code"] == "reauth_required"
    wrong = env.client.post("/api/v1/auth/reauth", json={"password": PASSWORD, "code": "000000"}, headers=bearer(token))
    assert wrong.status_code == 422
    ok = env.client.post("/api/v1/auth/reauth", json={"password": PASSWORD, "code": code(env, "owner")}, headers=bearer(token))
    assert ok.status_code == 200
    assert env.client.patch(f"/api/v1/users/{env.merch_id}", json={"is_active": False}, headers=bearer(token)).status_code == 200


# ------------------------------------------------------------------ владелец


def test_owner_is_protected_and_only_the_owner_grants_admin_or_resets_admin_2fa(env):
    owner = admin_token(env)
    created = env.client.post("/api/v1/users", json={"username": "second", "password": PASSWORD, "role": "admin"}, headers=bearer(owner))
    assert created.status_code == 201, created.text
    assert created.json()["must_change_password"] is True          # пароль, заданный администратором, — временный
    second_id = settle(env, created.json()["id"])
    second = admin_token(env, "second")
    assert env.client.get("/api/v1/auth/me", headers=bearer(second)).json()["is_owner"] is False
    for attempt in ({"password": "taken-over-pass"}, {"is_active": False}, {"role": "analyst"}):
        resp = env.client.patch(f"/api/v1/users/{env.owner_id}", json=attempt, headers=bearer(second))
        assert resp.status_code == 403 and resp.json()["code"] == "owner_protected", attempt
    assert env.client.post(f"/api/v1/users/{env.owner_id}/sessions/revoke-all", headers=bearer(second)).json()["code"] == "owner_protected"
    assert env.client.post(f"/api/v1/users/{env.owner_id}/2fa/reset", headers=bearer(second)).json()["code"] == "owner_protected"
    grant = env.client.post("/api/v1/users", json={"username": "third", "password": PASSWORD, "role": "admin"}, headers=bearer(second))
    assert grant.status_code == 403 and grant.json()["code"] == "owner_only"
    promote = env.client.patch(f"/api/v1/users/{env.merch_id}", json={"role": "admin"}, headers=bearer(second))
    assert promote.status_code == 403 and promote.json()["code"] == "owner_only"
    assert env.client.post(f"/api/v1/users/{second_id}/2fa/reset", headers=bearer(owner)).status_code == 200     # потерян телефон
    assert env.client.get("/api/v1/auth/me", headers=bearer(second)).status_code == 401
    assert login(env, "second").json()["mfa_enrollment_required"] is True
    assert env.client.post(f"/api/v1/users/{env.owner_id}/2fa/reset", headers=bearer(owner)).json()["code"] == "mfa_reset_self"


# ================================================================== аудит 2026-11-08 (документ 18, §12)


def client_at(e, base_url):
    """Другой адрес сервера (scope["server"]) и имя (Host) — как если бы запрос пришёл на другой сокет."""
    return TestClient(e.app, base_url=base_url)


def test_session_opened_on_the_employee_listener_has_no_admin_rights_anywhere(env):
    admin_token(env)                                                       # 2FA настроена на административном входе
    env.settings = env.settings.model_copy(update={"app_listener": "public"})
    step1 = login(env, "owner")                                            # пароль и код по Wi-Fi магазина
    lan = env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": step1.json()["mfa_token"], "code": code(env, "owner")})
    token = lan.json()["access_token"]
    env.settings = env.settings.model_copy(update={"app_listener": "admin"})
    denied = env.client.get("/api/v1/users", headers=bearer(token))        # тот же токен на административном входе
    assert denied.status_code == 403 and denied.json()["code"] == "admin_listener_required"
    assert env.client.get("/api/v1/auth/me", headers=bearer(token)).json()["admin_listener"] is False


def test_admin_listener_under_a_foreign_name_gives_no_admin_rights(env):
    token = admin_token(env)
    rebinding = client_at(env, "http://evil.example")                     # подмена DNS: чужое имя ведёт на localhost:8001
    denied = rebinding.get("/api/v1/users", headers=bearer(token))
    assert denied.status_code == 403 and denied.json()["code"] == "admin_listener_required"
    assert client_at(env, "http://100.64.0.5").get("/api/v1/users", headers=bearer(token)).status_code == 200   # адрес VPN
    with env.Session() as db:
        AuthService(db).create_user("second", PASSWORD, Role.ADMIN)
    refused = rebinding.post("/api/v1/auth/login", data={"username": "second", "password": PASSWORD})
    assert refused.status_code == 403 and refused.json()["code"] == "mfa_enrollment_required"   # пропуск настройки не выдаётся


def test_single_process_mode_gives_admin_rights_only_on_this_computer(env):
    env.settings = env.settings.model_copy(update={"app_listener": "all"})
    local = client_at(env, "http://127.0.0.1")
    first = local.post("/api/v1/auth/login", data={"username": "owner", "password": PASSWORD})
    assert first.json().get("mfa_enrollment_required") is True             # на самом компьютере настройка разрешена
    phone = client_at(env, "http://192.168.1.20")
    with env.Session() as db:
        AuthService(db).create_user("second", PASSWORD, Role.ADMIN)
    refused = phone.post("/api/v1/auth/login", data={"username": "second", "password": PASSWORD})
    assert refused.status_code == 403 and refused.json()["code"] == "mfa_enrollment_required"
    proxied = local.post("/api/v1/auth/login", data={"username": "second", "password": PASSWORD},
                         headers={"X-Forwarded-For": "203.0.113.9"})
    assert proxied.status_code == 403                                      # через обратный прокси — не «этот компьютер»


def test_password_and_code_checks_inside_a_session_are_throttled(env):
    """Украденный токен сотрудника не позволяет подбирать пароль через настройку второго фактора."""
    merch = merch_token(env)
    limit = env.settings.auth_max_failed_logins_per_user
    for _ in range(limit):
        wrong = env.client.post("/api/v1/auth/2fa/setup", json={"password": "not-the-password"}, headers=bearer(merch))
        assert wrong.status_code == 422 and wrong.json()["code"] == "password_wrong"
    blocked = env.client.post("/api/v1/auth/2fa/setup", json={"password": PASSWORD}, headers=bearer(merch))
    assert blocked.status_code == 429 and blocked.json()["code"] == "login_throttled"     # даже верный пароль — после паузы
    assert login(env, "merch").status_code == 429                                         # тот же счётчик, что у входа


@pytest.mark.parametrize("path", ["/api/v1/auth/reauth", "/api/v1/auth/2fa/recovery-codes"])
def test_reauth_and_new_recovery_codes_are_throttled(env, path):
    token = admin_token(env)
    for _ in range(env.settings.auth_max_failed_logins_per_user):
        env.client.post(path, json={"password": PASSWORD, "code": "000000"}, headers=bearer(token))
    blocked = env.client.post(path, json={"password": PASSWORD, "code": code(env, "owner")}, headers=bearer(token))
    assert blocked.status_code == 429


def test_collected_challenges_do_not_multiply_code_attempts(env):
    """Пароль известен: набрать много пропусков второго шага заранее — и пробовать по 5 кодов на каждый. Лимит по логину
    проверяется и во втором шаге."""
    enroll(env, "owner")
    passes = [login(env, "owner").json()["mfa_token"] for _ in range(4)]
    statuses = []
    for mfa_token in passes:
        for _ in range(3):
            statuses.append(env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": mfa_token, "code": "000000"}).status_code)
    assert statuses.count(429) >= len(statuses) - env.settings.auth_max_failed_logins_per_user
    assert 429 in statuses


def test_owner_sessions_are_not_visible_to_other_admins(env):
    owner = admin_token(env)
    created = env.client.post("/api/v1/users", json={"username": "second", "password": PASSWORD, "role": "admin"}, headers=bearer(owner))
    assert created.status_code == 201
    settle(env, created.json()["id"])
    second = admin_token(env, "second")
    peek = env.client.get(f"/api/v1/users/{env.owner_id}/sessions", headers=bearer(second))
    assert peek.status_code == 403 and peek.json()["code"] == "owner_protected"
    assert env.client.get(f"/api/v1/users/{env.owner_id}/sessions", headers=bearer(owner)).status_code == 200


def test_token_and_secret_responses_are_not_cacheable(env):
    _token, _codes, _ = enroll(env, "owner")
    resp = login(env, "merch")
    assert resp.headers["cache-control"] == "no-store"


def test_refresh_through_the_employee_listener_does_not_lift_the_8_hour_cap(env):
    with env.Session() as db:
        AuthService(db).create_user("analyst1", PASSWORD, Role.ANALYST)
    token = login(env, "analyst1").json()["access_token"]                 # не администратор, но вход административный
    with env.Session() as db:
        for meta in db.query(AuthSessionMeta).all():
            meta.auth_at = utcnow() - timedelta(hours=8)
        db.commit()
    env.settings = env.settings.model_copy(update={"app_listener": "public"})
    renewed = env.client.post("/api/v1/auth/refresh", headers=bearer(token))
    assert renewed.status_code == 401 and renewed.json()["code"] == "session_max_age"


def test_recovery_codes_are_per_user_single_use_replaced_on_regeneration_and_not_valid_for_reauth(env):
    owner, owner_codes, _ = enroll(env, "owner")
    created = env.client.post("/api/v1/users", json={"username": "second", "password": PASSWORD, "role": "admin"}, headers=bearer(owner))
    assert created.status_code == 201
    settle(env, created.json()["id"])
    _second, second_codes, _ = enroll(env, "second")
    foreign = env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": login(env, "second").json()["mfa_token"],
                                                             "code": owner_codes[0]})
    assert foreign.status_code == 422                                      # чужой код восстановления не подходит
    not_reauth = env.client.post("/api/v1/auth/reauth", json={"password": PASSWORD, "code": owner_codes[1]}, headers=bearer(owner))
    assert not_reauth.status_code == 422                                   # только для входа, не для подтверждения операций
    fresh = env.client.post("/api/v1/auth/2fa/recovery-codes", json={"password": PASSWORD, "code": code(env, "owner")},
                            headers=bearer(owner)).json()["recovery_codes"]
    old = env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": login(env, "owner").json()["mfa_token"], "code": owner_codes[2]})
    assert old.status_code == 422 and set(fresh).isdisjoint(owner_codes)   # прежний набор больше не действует
    with env.Session() as db:
        from app.db.models import AuthRecoveryCode
        stored = [r.code_hash for r in db.query(AuthRecoveryCode).all()]
    assert stored and not any(c.replace("-", "") in h or c in h for c in fresh + second_codes for h in stored)


def test_no_secret_reaches_the_server_log(env, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    token, codes, enroll_pass = enroll(env, "owner")
    used = env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": login(env, "owner").json()["mfa_token"], "code": codes[0]})
    assert used.status_code == 200
    env.client.post("/api/v1/auth/reauth", json={"password": "wrong-password-123", "code": "123456"}, headers=bearer(token))
    text = caplog.text
    assert "Вход по коду восстановления" in text                           # событие видно…
    for secret in [PASSWORD, "wrong-password-123", token, enroll_pass, used.json()["access_token"], env.secrets["owner"], *codes]:
        assert secret not in text, "секрет в журнале"                      # …а сам код, пароль, токен и ключ — нет


def test_legacy_token_without_session_details_has_no_admin_rights(env):
    """Токен из таблицы api_tokens без строки сведений (выдан до 2026-11-08): администратору — отказ всегда (нет второго фактора),
    сотруднику — работает как раньше, но прав управления не даёт."""
    from app.core.security import generate_token, hash_token
    from app.db.models import ApiToken
    raws = {}
    with env.Session() as db:
        for name, uid in (("owner", env.owner_id), ("merch", env.merch_id)):
            raws[name] = generate_token()
            db.add(ApiToken(token_hash=hash_token(raws[name]), user_id=uid, expires_at=utcnow() + timedelta(hours=1)))
        db.commit()
    assert env.client.get("/api/v1/auth/me", headers=bearer(raws["owner"])).status_code == 401
    me = env.client.get("/api/v1/auth/me", headers=bearer(raws["merch"]))
    assert me.status_code == 200 and me.json()["admin_listener"] is False


def test_console_reset_with_new_password_ends_sessions_and_old_password(env):
    from scripts import user_admin
    admin_token(env)
    out = []
    with env.Session() as db:
        code_ = user_admin.cmd_reset_2fa(db, "owner", assume_yes=True, new_password=True,
                                         getpass_fn=lambda _prompt: "brand-new-pass-42", out=out.append)
    assert code_ == 0 and "пароль изменён" in out[0]
    assert login(env, "owner").status_code == 401                          # старый пароль не действует
    fresh = login(env, "owner", password="brand-new-pass-42")
    assert fresh.json().get("mfa_enrollment_required") is True             # и второй фактор настраивается заново


def test_console_owner_recovery_when_the_owner_is_disabled(env):
    from scripts import user_admin
    owner = admin_token(env)
    created = env.client.post("/api/v1/users", json={"username": "second", "password": PASSWORD, "role": "admin"}, headers=bearer(owner))
    second_id = settle(env, created.json()["id"])
    out = []
    with env.Session() as db:
        assert user_admin.cmd_set_active(db, "owner", False, out=out.append) == 0
    assert any("--transfer" in line for line in out)                       # подсказка: передать права
    second = admin_token(env, "second")
    grant = env.client.post("/api/v1/users", json={"username": "third", "password": PASSWORD, "role": "admin"}, headers=bearer(second))
    assert grant.status_code == 403 and grant.json()["code"] == "owner_only"   # пока владелец отключён — назначать нельзя
    with env.Session() as db:
        assert user_admin.cmd_owner(db, "second", transfer=True, assume_yes=True, out=out.append) == 0
        assert AuthService(db).owner_id() == second_id
    second = admin_token(env, "second")                                    # роль владельца берётся из базы на каждом запросе
    assert env.client.get("/api/v1/auth/me", headers=bearer(second)).json()["is_owner"] is True


# ------------------------------------------------------------------ смена своего пароля (документ 05, §12.11)


def test_own_password_change_ends_every_session_and_gives_this_device_a_new_one(env):
    other = admin_token(env)
    current = admin_token(env)
    wrong = env.client.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "next-strong-pass",
                                                          "code": "000000"}, headers=bearer(current))
    assert wrong.status_code == 422 and wrong.json()["code"] == "mfa_code_wrong"
    recovery = env.client.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "next-strong-pass",
                                                             "code": "ABCDE-FGHJK"}, headers=bearer(current))
    assert recovery.status_code == 422                                    # коды восстановления — только для входа
    same = env.client.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": PASSWORD,
                                                         "code": code(env, "owner")}, headers=bearer(current))
    assert same.status_code == 422 and same.json()["code"] == "password_same"
    ok = env.client.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "next-strong-pass",
                                                       "code": code(env, "owner")}, headers=bearer(current))
    assert ok.status_code == 200, ok.text
    fresh = ok.json()["access_token"]
    assert env.client.get("/api/v1/auth/me", headers=bearer(current)).status_code == 401      # этот токен тоже заменён
    assert env.client.get("/api/v1/auth/me", headers=bearer(other)).status_code == 401
    me = env.client.get("/api/v1/auth/me", headers=bearer(fresh))
    assert me.status_code == 200 and me.json()["admin_listener"] is True                       # новый сеанс со вторым фактором
    assert login(env, "owner").status_code == 401
    assert login(env, "owner", password="next-strong-pass").json().get("mfa_required") is True
    with env.Session() as db:
        assert db.get(UserSecurity, env.owner_id).session_epoch >= 1


def test_own_password_change_without_second_factor_and_its_attempt_limit(env):
    merch = merch_token(env)
    for _ in range(env.settings.auth_max_failed_logins_per_user):
        bad = env.client.post("/api/v1/auth/password", json={"current_password": "not-it-123", "new_password": "next-strong-pass"},
                              headers=bearer(merch))
        assert bad.status_code == 422 and bad.json()["code"] == "password_wrong"
    blocked = env.client.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "next-strong-pass"},
                              headers=bearer(merch))
    assert blocked.status_code == 429
    routes_auth.login_throttle.reset()
    short = env.client.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "short"}, headers=bearer(merch))
    assert short.status_code == 422 and short.json()["code"] == "password_too_short"
    ok = env.client.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "next-strong-pass"},
                         headers=bearer(merch))
    assert ok.status_code == 200 and env.client.get("/api/v1/auth/me", headers=bearer(ok.json()["access_token"])).status_code == 200


# ================================================================== решения владельца 2026-11-08 21:53 (В-5, временный пароль)


def test_catalog_editing_is_refused_outside_the_admin_listener_by_the_server(env):
    """В-5: правка каталога — только на административном входе; решает сервер, а не страница."""
    import app.api.routes_catalog as routes_catalog
    env.app.include_router(routes_catalog.router)
    admin_token(env)
    env.settings = env.settings.model_copy(update={"app_listener": "public"})
    step1 = login(env, "owner")
    lan = env.client.post("/api/v1/auth/login/2fa", json={"mfa_token": step1.json()["mfa_token"], "code": code(env, "owner")}).json()
    token = lan["access_token"]
    me = env.client.get("/api/v1/auth/me", headers=bearer(token)).json()
    assert "catalog:write" not in me["permissions"] and "catalog:write" in me["role_permissions"]
    for method, path in (("post", "/api/v1/catalog/products"), ("post", "/api/v1/catalog/products/upload"),
                         ("post", "/api/v1/catalog/products/p1/photos"), ("patch", "/api/v1/catalog/products/p1")):
        resp = getattr(env.client, method)(path, headers=bearer(token))
        assert resp.status_code == 403 and resp.json()["code"] == "admin_listener_required", (path, resp.text)
    env.settings = env.settings.model_copy(update={"app_listener": "admin"})
    on_admin = admin_token(env)
    assert "catalog:write" in env.client.get("/api/v1/auth/me", headers=bearer(on_admin)).json()["permissions"]


def test_new_employee_gets_a_server_generated_temporary_password_good_only_for_setting_their_own(env):
    owner = admin_token(env)
    created = env.client.post("/api/v1/users", json={"username": "new.merch", "role": "merchandiser"}, headers=bearer(owner))
    assert created.status_code == 201, created.text
    body = created.json()
    temp = body["temporary_password"]
    assert body["must_change_password"] is True and len(temp) == 14 and body["temp_password_expires_at"]
    listed = [u for u in env.client.get("/api/v1/users", headers=bearer(owner)).json() if u["username"] == "new.merch"][0]
    assert listed["must_change_password"] is True and listed.get("temporary_password") is None      # в списке пароля нет
    first = login(env, "new.merch", password=temp)
    assert first.status_code == 200 and first.json()["password_change_required"] is True and "access_token" not in first.json()
    pass_token = first.json()["password_token"]
    assert env.client.get("/api/v1/auth/me", headers=bearer(pass_token)).status_code == 401           # пропуск — не сеанс
    assert env.client.get("/api/v1/auth/sessions", headers=bearer(pass_token)).status_code == 401
    assert env.client.post("/api/v1/auth/refresh", headers=bearer(pass_token)).status_code == 401
    same = env.client.post("/api/v1/auth/password/initial", json={"new_password": temp}, headers=bearer(pass_token))
    assert same.status_code == 422 and same.json()["code"] == "password_same"
    as_login = env.client.post("/api/v1/auth/password/initial", json={"new_password": "New.Merch"}, headers=bearer(pass_token))
    assert as_login.status_code == 422 and as_login.json()["code"] in ("password_is_login", "password_too_short")
    ok = env.client.post("/api/v1/auth/password/initial", json={"new_password": "own-merch-pass"}, headers=bearer(pass_token))
    assert ok.status_code == 200 and ok.json()["access_token"]
    assert env.client.get("/api/v1/auth/me", headers=bearer(ok.json()["access_token"])).status_code == 200
    assert env.client.post("/api/v1/auth/password/initial", json={"new_password": "again-pass-1"},
                           headers=bearer(pass_token)).status_code == 401                              # пропуск одноразовый
    assert login(env, "new.merch", password=temp).status_code == 401                                   # временный больше не действует
    assert "access_token" in login(env, "new.merch", password="own-merch-pass").json()


def test_expired_temporary_password_is_reported_only_after_the_correct_password(env):
    owner = admin_token(env)
    temp = env.client.post("/api/v1/users", json={"username": "late.user", "role": "merchandiser"},
                           headers=bearer(owner)).json()["temporary_password"]
    with env.Session() as db:
        uid = AuthService(db).list_users()[[u.username for u in AuthService(db).list_users()].index("late.user")].id
        sec = db.get(UserSecurity, uid)
        sec.temp_password_expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    expired = login(env, "late.user", password=temp)
    assert expired.status_code == 403 and expired.json()["code"] == "temp_password_expired"
    wrong = login(env, "late.user", password="WRONG-PASS-0000")
    assert wrong.status_code == 401 and wrong.json()["code"] == "login_failed"                        # общий ответ
    fresh = env.client.post(f"/api/v1/users/{uid}/temporary-password", headers=bearer(owner))
    assert fresh.status_code == 200 and login(env, "late.user", password=fresh.json()["temporary_password"]).json()["password_change_required"]


def test_password_set_by_an_admin_is_temporary_and_own_password_is_not_changed_through_user_management(env):
    merch = merch_token(env)
    owner = admin_token(env)
    patched = env.client.patch(f"/api/v1/users/{env.merch_id}", json={"password": "typed-by-admin-1"}, headers=bearer(owner))
    assert patched.status_code == 200 and patched.json()["must_change_password"] is True
    assert env.client.get("/api/v1/auth/me", headers=bearer(merch)).status_code == 401                # сеансы завершены
    assert login(env, "merch", password="typed-by-admin-1").json()["password_change_required"] is True
    own = env.client.patch(f"/api/v1/users/{env.owner_id}", json={"password": "another-pass-1"}, headers=bearer(owner))
    assert own.status_code == 400 and own.json()["code"] == "own_password_in_settings"
    self_temp = env.client.post(f"/api/v1/users/{env.owner_id}/temporary-password", headers=bearer(owner))
    assert self_temp.status_code in (400, 403)


def test_admin_with_second_factor_needs_the_code_to_replace_a_temporary_password_and_then_signs_in_again(env):
    owner = admin_token(env)
    created = env.client.post("/api/v1/users", json={"username": "second", "role": "admin"}, headers=bearer(owner)).json()
    second_id = created["id"]
    first = login(env, "second", password=created["temporary_password"])
    pass_token = first.json()["password_token"]
    assert first.json()["code_required"] is False
    done = env.client.post("/api/v1/auth/password/initial", json={"new_password": "second-own-pass"}, headers=bearer(pass_token))
    assert done.status_code == 200 and done.json() == {"status": "password_set", "next": "login"}       # 2FA ещё нет — сеанса нет
    assert login(env, "second", password="second-own-pass").json()["mfa_enrollment_required"] is True
    env.secrets.pop("second", None)
    enrolled = admin_token(env, "second", password="second-own-pass")                                   # настроил 2FA
    assert enrolled
    issued = env.client.post(f"/api/v1/users/{second_id}/temporary-password", headers=bearer(owner))
    assert issued.status_code == 200                                                                    # только владелец — администратору
    step = login(env, "second", password=issued.json()["temporary_password"]).json()
    assert step["code_required"] is True
    no_code = env.client.post("/api/v1/auth/password/initial", json={"new_password": "second-pass-2"}, headers=bearer(step["password_token"]))
    assert no_code.status_code == 422 and no_code.json()["code"] == "mfa_code_wrong"
    with_code = env.client.post("/api/v1/auth/password/initial", json={"new_password": "second-pass-2", "code": code(env, "second")},
                                headers=bearer(step["password_token"]))
    assert with_code.status_code == 200 and with_code.json()["status"] == "password_set"
    assert login(env, "second", password="second-pass-2").json()["mfa_required"] is True


def test_non_owner_admin_cannot_issue_temporary_passwords_to_admins_or_the_owner(env):
    owner = admin_token(env)
    for name in ("second", "third"):
        created = env.client.post("/api/v1/users", json={"username": name, "password": PASSWORD, "role": "admin"}, headers=bearer(owner))
        settle(env, created.json()["id"])
    third_id = settle(env, None, username="third")
    second = admin_token(env, "second")
    on_owner = env.client.post(f"/api/v1/users/{env.owner_id}/temporary-password", headers=bearer(second))
    assert on_owner.status_code == 403 and on_owner.json()["code"] == "owner_protected"
    on_admin = env.client.post(f"/api/v1/users/{third_id}/temporary-password", headers=bearer(second))
    assert on_admin.status_code == 403 and on_admin.json()["code"] == "owner_only"
    on_merch = env.client.post(f"/api/v1/users/{env.merch_id}/temporary-password", headers=bearer(second))
    assert on_merch.status_code == 200                                     # сотруднику — любой администратор


def test_bootstrap_owner_password_from_env_is_temporary_without_expiry_and_no_session_is_accepted_meanwhile(env):
    from scripts import bootstrap
    with env.Session() as db:
        assert bootstrap.create_admin(db, "founder", "founder-env-pass", temporary=True) == "created"
        service = AuthService(db)
        founder = service.list_users()[[u.username for u in service.list_users()].index("founder")]
        must, deadline = service.temporary_password_state(founder.id)
        assert must is True and deadline is None
        forged, _ = service.issue_token(founder, 60, mfa=True)                 # обычный сеанс, выданный в обход входа
    assert env.client.get("/api/v1/auth/me", headers=bearer(forged)).status_code == 401
    step = login(env, "founder", password="founder-env-pass").json()
    assert step["password_change_required"] is True
    done = env.client.post("/api/v1/auth/password/initial", json={"new_password": "founder-own-pass"}, headers=bearer(step["password_token"]))
    assert done.json()["status"] == "password_set"
    assert login(env, "founder", password="founder-env-pass").status_code == 401
    assert login(env, "founder", password="founder-own-pass").json()["mfa_enrollment_required"] is True
