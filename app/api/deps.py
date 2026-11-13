"""
FastAPI-зависимости авторизации (F13) — тонкий слой между HTTP и чистой логикой
из app/core/security.py.

Как этим пользоваться в роуте:

    user: CurrentUser = Depends(require_permission(Permission.REPORT_READ))
    ensure_store_access(user, store_id)          # если роут привязан к магазину

Коды ответов:
  401 — токена нет / он недействителен / просрочен / учётка отключена / сеанс администратора без второго фактора;
  403 — токен верный, но у роли нет такого права, либо магазин вне скоупа пользователя;
  403 admin_listener_required — права управления здесь не действуют (решение В-1: только через VPN или на самом компьютере,
        административный порт, и только в сеансе, открытом там же; app/core/mfa_policy.py, admin_rights_apply);
  403 reauth_required — важная операция: подтвердите пароль и код (POST /api/v1/auth/reauth), затем повторите.

Других путей проверки токена нет: все маршруты берут пользователя только отсюда (статический тест tests/test_auth_invariants.py).
"""
from __future__ import annotations

from fastapi import Depends, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.mfa_policy import (
    SCOPE_ENROLL,
    SCOPE_FULL,
    SCOPE_PASSWORD,
    admin_rights_apply,
    listener_for_request,
    recently_authenticated,
)
from app.core.security import ADMIN_ONLY_PERMISSIONS, CurrentUser, Permission, utcnow
from app.db.session import get_db
from app.services.auth_service import AuthService

# tokenUrl нужен только для кнопки «Authorize» в Swagger UI (/docs): логин там идёт
# form-encoded на этот же адрес (см. routes_auth.login).
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def unauthorized(detail: str = "Недействительный или просроченный токен", code: str = "token_invalid") -> ApiError:
    return ApiError(status.HTTP_401_UNAUTHORIZED, code, detail, headers={"WWW-Authenticate": "Bearer"})


# Разрешения управления: действуют только на административном входе программы (решения В-1 и В-5). Проверка — здесь, на уровне права,
# а не по списку адресов: новый маршрут с этим правом защищён сразу, обойти через «другой адрес API» нельзя. Состав — все права,
# которые есть только у администратора (security.ADMIN_ONLY_PERMISSIONS): пользователи, магазины и полки, камеры, правка каталога.
ADMIN_PERMISSIONS: frozenset[Permission] = ADMIN_ONLY_PERMISSIONS


def request_listener(request: Request) -> str:
    """Вход программы для ЭТОГО запроса: admin или public (mfa_policy.listener_for_request). Учитываются настройка APP_LISTENER,
    адрес сокета сервера, на который пришёл запрос (scope["server"]; клиент его не подделает), заголовок Host и заголовки прокси."""
    settings = get_settings()
    return listener_for_request(settings.app_listener, request.scope, request.headers, vpn_bind_ip=settings.vpn_bind_ip,
                                extra_hosts=settings.admin_allowed_hosts)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """Обычный сеанс. Пропуск настройки второго фактора и сеанс администратора без второго фактора сюда не проходят."""
    user = AuthService(db).resolve_token(token, require_admin_mfa=get_settings().auth_require_2fa_for_admins)
    if user is None:
        raise unauthorized()
    return user


def get_enrolling_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """Только для настройки второго фактора (/auth/2fa, /auth/2fa/setup, /auth/2fa/confirm): обычный сеанс или пропуск настройки."""
    user = AuthService(db).resolve_token(token, require_admin_mfa=get_settings().auth_require_2fa_for_admins,
                                         allow_scopes=(SCOPE_FULL, SCOPE_ENROLL))
    if user is None:
        raise unauthorized()
    return user


def get_password_changing_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """Только для POST /auth/password/initial: пропуск «смена временного пароля» (документ 05, §12.2). Обычный сеанс сюда не
    проходит — свой пароль в открытом сеансе меняют через /auth/password (с текущим паролем)."""
    user = AuthService(db).resolve_token(token, require_admin_mfa=get_settings().auth_require_2fa_for_admins,
                                         allow_scopes=(SCOPE_PASSWORD,))
    if user is None:
        raise unauthorized()
    return user


def admin_listener_error() -> ApiError:
    return ApiError(status.HTTP_403_FORBIDDEN, "admin_listener_required",
                    "Управление доступно только через VPN или на самом компьютере магазина (административный адрес программы)")


def require_permission(permission: Permission):
    """Фабрика зависимости: пропускает только роли, у которых есть указанное право; права управления — только на
    административном входе программы."""

    def dependency(request: Request, user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has_permission(permission):
            raise ApiError(status.HTTP_403_FORBIDDEN, "forbidden", "Недостаточно прав для этого действия")
        # Права управления: запрос пришёл на административный вход И сеанс открыт там же (mfa_policy.admin_rights_apply).
        if permission in ADMIN_PERMISSIONS and not admin_rights_apply(request_listener(request), user.session_listener):
            raise admin_listener_error()
        return user

    return dependency


def require_recent_auth(permission: Permission):
    """Важная операция: право (с проверкой входа программы) и подтверждение личности не старше auth_reauth_minutes."""
    base = require_permission(permission)

    def dependency(user: CurrentUser = Depends(base)) -> CurrentUser:
        if not recently_authenticated(user.reauth_at, utcnow(), get_settings().auth_reauth_minutes):
            raise ApiError(status.HTTP_403_FORBIDDEN, "reauth_required",
                           "Подтвердите пароль и код из приложения, затем повторите действие")
        return user

    return dependency


def ensure_store_access(user: CurrentUser, store_id: str) -> None:
    if not user.can_access_store(store_id):
        raise ApiError(status.HTTP_403_FORBIDDEN, "store_forbidden", "Нет доступа к этому магазину")
