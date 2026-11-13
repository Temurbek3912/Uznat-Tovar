"""Вход администратора в API-тестах так же, как в рабочей программе (решения В-1…В-3, документ 18): административный вход
программы, адрес этого компьютера, пароль и код второго фактора. Ничего в настройках безопасности не ослабляется: второй фактор
по-прежнему обязателен, просто тест настраивает его администратору заранее.

    settings = admin_entry(monkeypatch)                    # ключ секретов + административный вход
    client = TestClient(app, base_url="http://localhost")  # адрес этого компьютера (защита от подмены DNS)
    enable_totp(Session, "admin", settings)                # у администратора включён второй фактор
    token = admin_token(client, Session, "admin", PASSWORD, settings)
"""
from __future__ import annotations

import time

import app.api.deps as deps
import app.api.routes_auth as routes_auth
from app.core import totp
from app.core.crypto import generate_key
from app.db.models import User, UserSecurity
from app.services.auth_service import AuthService

_SECRETS: dict[tuple[int, str], str] = {}


def admin_entry(monkeypatch, **overrides):
    """Настройки теста: ключ для секретов второго фактора и административный вход (порт 8001 в рабочей программе)."""
    settings = routes_auth.get_settings().model_copy(update={
        "auth_secret_key": generate_key(), "auth_require_2fa_for_admins": True, "app_listener": "admin",
        "vpn_bind_ip": "", "admin_allowed_hosts": "", **overrides})
    monkeypatch.setattr(routes_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    return settings


def enable_totp(Session, username: str, settings) -> str:
    """Включить второй фактор пользователю (как после «Настройки → Безопасность входа»). -> секрет приложения-аутентификатора."""
    with Session() as db:
        service = AuthService(db)
        user = db.query(User).filter(User.username == username).one()
        secret = service.begin_totp_setup(user.id, None, settings.auth_secret_key, password_verified=True)["secret"]
        service.confirm_totp_setup(user.id, totp.code_at(secret, time.time()), settings.auth_secret_key)
    _SECRETS[(id(Session), username)] = secret
    return secret


def admin_token(client, Session, username: str, password: str, settings=None) -> str:
    """Вход в два шага: пароль -> код. Защита от повтора кода в рабочей программе действует; тест, входящий несколько раз
    за 30 секунд, сбрасывает только отметку «последний принятый шаг» своего пользователя в своей временной базе."""
    secret = _SECRETS[(id(Session), username)]
    with Session() as db:
        user = db.query(User).filter(User.username == username).one()
        security = db.get(UserSecurity, user.id)
        security.totp_last_step = None
        db.commit()
    first = client.post("/api/v1/auth/login", data={"username": username, "password": password})
    assert first.status_code == 200 and first.json().get("mfa_required") is True, first.text
    second = client.post("/api/v1/auth/login/2fa",
                         json={"mfa_token": first.json()["mfa_token"], "code": totp.code_at(secret, time.time())})
    assert second.status_code == 200, second.text
    return second.json()["access_token"]
