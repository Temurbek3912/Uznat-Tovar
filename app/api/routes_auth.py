"""
Вход/выход и управление пользователями (F13).

  POST  /api/v1/auth/login    — логин+пароль (form-encoded, стандарт OAuth2) -> токен
                                 либо {"mfa_required": true, "mfa_token", "expires_in"}, если включён второй фактор
  POST  /api/v1/auth/login/2fa — второй шаг входа: {"mfa_token", "code"} -> токен
  POST  /api/v1/auth/logout   — отзывает переданный токен
  POST  /api/v1/auth/logout-all — выйти на всех устройствах (новое поколение сеансов; этот токен тоже перестаёт действовать)
  GET   /api/v1/auth/me       — кто я, какие права и магазины (+ mfa_enabled)
  GET   /api/v1/auth/sessions — мои действующие сеансы (устройство, откуда, вход, активность; без IP и токенов)
  DELETE /api/v1/auth/sessions/{id} — завершить свой сеанс;  POST /api/v1/auth/sessions/revoke-others — все, кроме этого
  GET   /api/v1/auth/2fa      — состояние второго фактора;   POST /auth/2fa/setup {password} -> секрет и otpauth://
  POST  /api/v1/auth/2fa/confirm {code} — включить;        POST /auth/2fa/disable {password, code} — выключить
  POST  /api/v1/auth/password {current_password, new_password, code?} — сменить свой пароль -> новый токен (остальные сеансы завершены)
  POST  /api/v1/users         — создать пользователя            (только admin)
  GET   /api/v1/users         — список пользователей            (только admin)
  PATCH /api/v1/users/{id}    — роль / активность / магазины / пароль (только admin)
  GET   /api/v1/users/{id}/sessions — сеансы пользователя; POST /users/{id}/sessions/revoke-all — завершить все (только admin)

Вход с разных устройств и удалённый вход администратора — тот же путь (документ 18): один токен api_tokens, одна проверка
AuthService.resolve_token. Канал защищает HTTPS (обратный прокси или VPN, документ 18) — приложение отдаёт HSTS, когда запрос
пришёл по HTTPS (app/core/security_headers.py).

Первого администратора создаёт скрипт `python -m scripts.create_user` — через API
это невозможно по построению (создавать пользователей может только админ).

Эндпоинты входа — обычные `def`, а не `async def`: scrypt намеренно медленный
(десятки мс CPU), и в async-функции он блокировал бы весь event loop.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    admin_listener_error,
    get_current_user,
    get_enrolling_user,
    get_password_changing_user,
    oauth2_scheme,
    request_listener,
    require_permission,
    require_recent_auth,
    unauthorized,
)
from app.core import secure_transport, temp_password
from app.core.config import get_settings
from app.core.errors import ApiError, from_app_error
from app.core.mfa_policy import (
    ASK_CODE,
    ENROLL,
    ENROLL_ELSEWHERE,
    ENROLL_TTL_MINUTES,
    ISSUE,
    LISTENER_ADMIN,
    LISTENER_PUBLIC,
    SCOPE_ENROLL,
    SCOPE_FULL,
    SCOPE_PASSWORD,
    admin_rights_apply,
    after_password,
    owner_guard,
    session_lifetime_minutes,
)
from app.core.security import (
    ADMIN_ONLY_PERMISSIONS,
    ALL_STORES_ROLES,
    ROLE_PERMISSIONS,
    CurrentUser,
    Permission,
    PolicyViolation,
    Role,
    check_password_policy,
    parse_role,
    utcnow,
)
from app.core.throttle import LoginThrottle
from app.db.models import User
from app.db.session import get_db
from app.services.auth_service import (
    AuthService,
    AuthServiceError,
    LoginContext,
    UnknownStoreError,
    UserAlreadyExistsError,
    UserNotFoundError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["auth"])

_settings = get_settings()
# Один экземпляр на процесс: счётчики живут в памяти (см. ограничения в app/core/throttle.py).
login_throttle = LoginThrottle(
    max_failures_per_user=_settings.auth_max_failed_logins_per_user,
    max_failures_per_ip=_settings.auth_max_failed_logins_per_ip,
    window_seconds=_settings.auth_lockout_window_minutes * 60,
)


class TokenResponse(BaseModel):
    """Обычный сеанс. recovery_left — если вход по коду восстановления: сколько кодов осталось."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # секунды до истечения токена (стандартное поле OAuth2)
    username: str
    role: Role
    recovery_left: int | None = None


class MfaRequiredResponse(BaseModel):
    """Пароль верный, нужен второй шаг: код из приложения-аутентификатора. Токена сеанса ещё нет."""

    mfa_required: bool = True
    mfa_token: str            # одноразовый пропуск второго шага (в базе — только хеш)
    expires_in: int           # секунды на ввод кода
    username: str


class MfaLoginRequest(BaseModel):
    mfa_token: str = Field(min_length=10, max_length=200)
    code: str = Field(max_length=20)          # 6 цифр из приложения или код восстановления «XXXXX-XXXXX»


class EnrollRequiredResponse(BaseModel):
    """Администратор без второго фактора (решение В-3): пропуск настройки — токен, годный ТОЛЬКО для /auth/2fa/setup и
    /auth/2fa/confirm, 10 минут. Выдаётся только на административном входе программы (решение В-1)."""

    mfa_enrollment_required: bool = True
    enroll_token: str
    expires_in: int
    username: str


class TokenWithRecovery(BaseModel):
    """Второй фактор включён: обычный сеанс и коды восстановления (показываются ОДИН раз)."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    username: str
    role: Role
    recovery_codes: list[str]


class ReauthRequest(BaseModel):
    password: str = Field(max_length=200)
    code: str | None = Field(default=None, max_length=20)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)
    code: str | None = Field(default=None, max_length=20)   # если включён второй фактор


class PasswordChangeRequiredResponse(BaseModel):
    """Вход временным паролем: обычного сеанса нет, только пропуск на установку своего пароля (документ 05, §12.2)."""
    password_change_required: bool = True
    password_token: str
    expires_in: int
    username: str
    code_required: bool = False      # включён второй фактор — при установке пароля нужен и код


class InitialPassword(BaseModel):
    new_password: str = Field(max_length=200)
    code: str | None = Field(default=None, max_length=20)


class PasswordSetResponse(BaseModel):
    status: str = "password_set"
    next: str = "login"              # войти с новым паролем (второй фактор или его настройка — как при обычном входе)


class TemporaryPasswordOut(BaseModel):
    temporary_password: str          # показывается ОДИН раз тому, кто выдал; в базе — только хеш
    expires_at: datetime | None


class MeResponse(BaseModel):
    username: str
    role: Role
    all_stores: bool          # True для admin/analyst — им доступна вся сеть
    store_ids: list[str]      # назначенные магазины (для admin/analyst не ограничивают доступ)
    permissions: list[Permission]   # права, которые действуют ЗДЕСЬ: без прав «только администратор» вне административного входа
    role_permissions: list[Permission] = []   # все права роли (справочно; страницы решают по permissions)
    mfa_enabled: bool = False  # включён ли второй фактор (новое поле; прежние клиенты его не читают)
    is_owner: bool = False     # владелец системы (назначается только с консоли сервера)
    admin_listener: bool = True  # действуют ли здесь права управления (решение В-1: только административный вход)
    language: str | None = None  # язык интерфейса из профиля (миграция 0002); None — не выбран, страница берёт язык браузера


# Коды языков интерфейса — те же, что во frontend/i18n.js (LANGS). Четыре языковых варианта (решение владельца 2026-11-06).
LANGUAGES = ("ru", "uz", "uz-Cyrl", "en")


class LanguageIn(BaseModel):
    language: str | None = Field(default=None, max_length=8)


class SessionOut(BaseModel):
    id: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    device_label: str | None
    device_kind: str
    device_os: str
    device_browser: str
    source: str               # local — этот компьютер, lan — локальная сеть, remote — интернет, unknown
    mfa: bool                 # вход со вторым фактором
    current: bool


class PasswordConfirm(BaseModel):
    password: str | None = Field(default=None, max_length=200)   # для пропуска настройки не нужен (пароль проверен при входе)


class CodeConfirm(BaseModel):
    code: str = Field(max_length=20)


class PasswordAndCode(BaseModel):
    password: str = Field(max_length=200)
    code: str = Field(max_length=20)


class UserCreate(BaseModel):
    username: str
    # Пароль необязателен: не задан — сервер выдаёт временный и показывает его ОДИН раз (документ 05, §12.2). Задан (прежние
    # клиенты) — он тоже временный: его знает администратор, сотрудник обязан сменить его при первом входе.
    password: str | None = None
    role: Role
    store_ids: list[str] = Field(default_factory=list)


class UserUpdate(BaseModel):
    role: Role | None = None
    is_active: bool | None = None
    store_ids: list[str] | None = None   # передан список -> заменяет текущий набор магазинов целиком
    password: str | None = None


class UserOut(BaseModel):
    id: str
    username: str
    role: str
    is_active: bool
    store_ids: list[str]
    created_at: datetime
    must_change_password: bool = False            # «ждёт первого входа»: действует временный пароль
    temp_password_expires_at: datetime | None = None
    temporary_password: str | None = None         # только в ответе на создание, если пароль выдал сервер, — ОДИН раз


def _user_out(user: User, service: AuthService | None = None) -> UserOut:
    must_change, deadline = service.temporary_password_state(user.id) if service is not None else (False, None)
    return UserOut(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        store_ids=sorted(a.store_id for a in user.store_access),
        created_at=user.created_at,
        must_change_password=must_change,
        temp_password_expires_at=deadline,
    )


# --------------------------------------------------------------------- вход/выход


def _login_context(request: Request) -> LoginContext:
    """Откуда вход: адрес (настоящий — только при uvicorn --proxy-headers за доверенным прокси), браузер, название устройства."""
    return LoginContext(ip=request.client.host if request.client else None,
                        user_agent=request.headers.get("user-agent"),
                        device_label=request.headers.get("x-device-name"))


def _ttl_minutes(role: Role, listener: str) -> int:
    settings = get_settings()
    return session_lifetime_minutes(base_ttl_minutes=settings.auth_token_ttl_minutes, is_admin=role == Role.ADMIN,
                                    listener=listener, admin_max_hours=settings.auth_admin_session_max_hours)


def _throttle_check(username: str, client_ip: str | None) -> None:
    """Общее ограничение неудачных попыток (по логину и адресу) — для входа и для ЛЮБОЙ проверки пароля или кода в открытом
    сеансе: иначе украденный токен позволял бы подбирать пароль и код через подтверждение операций (аудит 2026-11-08)."""
    retry_after = login_throttle.check(username, client_ip)
    if retry_after > 0:
        raise ApiError(status.HTTP_429_TOO_MANY_REQUESTS, "login_throttled",
                       "Слишком много неудачных попыток входа. Повторите позже",
                       {"retry_after": retry_after}, headers={"Retry-After": str(retry_after)})


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _guarded(request: Request, username: str, action):
    """Проверка пароля или кода в открытом сеансе: сначала лимит, неверный пароль или код (вид ошибки «invalid») считается
    неудачной попыткой входа. Ошибки сервиса превращаются в ответ API как обычно."""
    client_ip = _client_ip(request)
    _throttle_check(username, client_ip)
    try:
        return action()
    except AuthServiceError as exc:
        if getattr(exc, "kind", "") == "invalid":
            login_throttle.record_failure(username, client_ip)
        raise from_app_error(exc) from exc


def _issue(service: AuthService, user: User, role: Role, request: Request, *, mfa: bool,
           recovery_left: int | None = None) -> TokenResponse:
    """Единственное место выдачи обычного сеанса (вход, второй шаг, завершение настройки 2FA) — tests/test_auth_invariants.py.
    Вход программы сеанса — вход ЭТОГО запроса (admin или public): права управления потом действуют только при совпадении."""
    settings = get_settings()
    listener = request_listener(request)
    ttl = _ttl_minutes(role, listener)
    token, _expires_at = service.issue_token(user, ttl, context=_login_context(request), mfa=mfa,
                                             secret_key=settings.auth_secret_key, scope=SCOPE_FULL, listener=listener)
    return TokenResponse(access_token=token, expires_in=ttl * 60, username=user.username, role=role, recovery_left=recovery_left)


# PasswordChangeRequiredResponse обязательно в response_model: без него FastAPI не может проверить ответ на вход временным
# паролем и отдаёт 500 (найдено первым настоящим прогоном API-тестов 2026-11-13).
@router.post("/auth/login",
             response_model=TokenResponse | MfaRequiredResponse | EnrollRequiredResponse | PasswordChangeRequiredResponse)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> TokenResponse | MfaRequiredResponse | EnrollRequiredResponse | PasswordChangeRequiredResponse:
    # За reverse-proxy request.client — адрес прокси, и лимит по IP накрыл бы всех сразу. Настоящий
    # адрес клиента подставляет uvicorn при запуске с --proxy-headers (--forwarded-allow-ips=<IP прокси>);
    # X-Forwarded-For здесь вручную НЕ читаем — клиент подделал бы его и обходил лимит.
    client_ip = _client_ip(request)

    # Лимит проверяем ДО проверки пароля: заблокированному отказ отдаётся без scrypt (нет нагрузки на
    # CPU), а такие отказы не записываются как неудачи — блокировку нельзя продлевать потоком запросов.
    _throttle_check(form.username, client_ip)

    service = AuthService(db)
    user = service.authenticate(form.username, form.password)
    if user is None:
        login_throttle.record_failure(form.username, client_ip)
        # Одно и то же сообщение для «нет такого логина» и «неверный пароль» — не выдаём, что из двух.
        raise unauthorized("Неверный логин или пароль", code="login_failed")

    role = parse_role(user.role)
    if role is None:
        raise ApiError(status.HTTP_403_FORBIDDEN, "role_unknown", "Роль пользователя не распознана — обратитесь к администратору")

    settings = get_settings()
    listener = request_listener(request)
    # Временный пароль (документ 05, §12.2): сеанса нет — только пропуск «смена пароля» на 15 минут. Истёкший — отказ, но лишь
    # после ВЕРНОГО пароля (при неверном — общий ответ выше).
    must_change, temp_deadline = service.temporary_password_state(user.id)
    if must_change:
        if temp_password.is_expired(temp_deadline, utcnow()):
            raise ApiError(status.HTTP_403_FORBIDDEN, "temp_password_expired",
                           "Временный пароль истёк. Обратитесь к администратору за новым")
        login_throttle.record_success(form.username)
        pass_token, _ = service.issue_token(user, temp_password.PASS_TTL_MINUTES, context=_login_context(request), mfa=False,
                                            secret_key=settings.auth_secret_key, scope=SCOPE_PASSWORD, listener=listener)
        return PasswordChangeRequiredResponse(password_token=pass_token, expires_in=temp_password.PASS_TTL_MINUTES * 60,
                                              username=user.username, code_required=service.totp_enabled(user.id))
    decision = after_password(totp_enabled=service.totp_enabled(user.id), is_admin=role == Role.ADMIN,
                              require_for_admins=settings.auth_require_2fa_for_admins, listener=listener)
    if decision == ASK_CODE:                      # счётчик неудач по логину сбрасывается только после верного кода
        mfa_token, expires_in = service.start_mfa_challenge(user, settings.auth_mfa_challenge_minutes)
        return MfaRequiredResponse(mfa_token=mfa_token, expires_in=expires_in, username=user.username)
    if decision == ENROLL_ELSEWHERE:
        raise ApiError(status.HTTP_403_FORBIDDEN, "mfa_enrollment_required",
                       "Администратору нужен второй фактор. Настройте его на самом компьютере магазина или через VPN "
                       "(административный адрес программы), раздел «Настройки» → «Безопасность входа»")
    login_throttle.record_success(form.username)
    if decision == ENROLL:
        token, _ = service.issue_token(user, ENROLL_TTL_MINUTES, context=_login_context(request), mfa=False,
                                       secret_key=settings.auth_secret_key, scope=SCOPE_ENROLL, listener=listener)
        return EnrollRequiredResponse(enroll_token=token, expires_in=ENROLL_TTL_MINUTES * 60, username=user.username)
    return _issue(service, user, role, request, mfa=False)


@router.get("/auth/transport")
def transport(request: Request) -> dict:
    """Можно ли отсюда отправлять пароль, код и токен (документ 18, §14). Страница спрашивает это ДО отправки пароля. Без секретов,
    без сеанса; этот адрес не отклоняется по незащищённому каналу (иначе страница не узнала бы, куда идти)."""
    settings = get_settings()
    return secure_transport.describe(secure=secure_transport.secure_for_scope(request.scope, settings),
                                     required=settings.auth_require_secure_transport, https_url=settings.public_https_url)


@router.post("/auth/login/2fa", response_model=TokenResponse)
def login_second_step(request: Request, body: MfaLoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Второй шаг входа. Неверный код учитывается в общем ограничении попыток входа (по логину и адресу); лимит по логину
    проверяется и здесь — иначе заранее набранные пропуски (пароль известен) давали бы по 5 попыток кода каждый."""
    client_ip = _client_ip(request)
    settings = get_settings()
    service = AuthService(db)
    _throttle_check(service.mfa_challenge_username(body.mfa_token) or "", client_ip)
    try:
        result = service.complete_mfa_challenge(body.mfa_token, body.code, settings.auth_secret_key,
                                                max_attempts=settings.auth_mfa_max_attempts)
    except AuthServiceError as exc:
        # неверный код (422, осталось попыток) и исчерпанные попытки (401) считаются неудачей входа по логину и адресу;
        # истёкший пропуск и отсутствие ключа сервера (503) — нет
        if getattr(exc, "username", None):
            login_throttle.record_failure(exc.username, client_ip)
        raise from_app_error(exc) from exc
    user = result.user
    role = parse_role(user.role)
    if role is None:
        raise ApiError(status.HTTP_403_FORBIDDEN, "role_unknown", "Роль пользователя не распознана — обратитесь к администратору")
    login_throttle.record_success(user.username)
    if result.recovery_used:
        logger.warning("Вход по коду восстановления второго фактора: %s (осталось кодов: %s)", user.username, result.recovery_left)
    return _issue(service, user, role, request, mfa=True, recovery_left=result.recovery_left)


@router.post("/auth/refresh", response_model=TokenResponse)
def refresh(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> TokenResponse:
    """Продление сеанса обменом токена: новый токен, старый перестаёт действовать сразу. Предел от входа (В-4) не сдвигается:
    достигнут — 401 session_max_age, нужно войти заново с паролем и кодом. Предел — по сеансу (роль и вход, где он открыт),
    а не по входу, через который продлевают: сеанс административного входа не продлить дольше 8 часов через порт сотрудников."""
    settings = get_settings()
    session_listener = LISTENER_ADMIN if user.session_listener == LISTENER_ADMIN else LISTENER_PUBLIC
    ttl = _ttl_minutes(user.role, session_listener)
    capped = user.role == Role.ADMIN or session_listener == LISTENER_ADMIN
    max_hours = settings.auth_admin_session_max_hours if capped else None
    renewed = AuthService(db).refresh_session(user.session_id, ttl, max_hours=max_hours)
    if renewed is None:
        raise unauthorized("Сеанс достиг предельной длительности — войдите заново", code="session_max_age")
    token, expires_at = renewed
    seconds = max(60, int((expires_at - utcnow()).total_seconds()))
    return TokenResponse(access_token=token, expires_in=seconds, username=user.username, role=user.role)


@router.post("/auth/reauth")
def reauth(body: ReauthRequest, request: Request, user: CurrentUser = Depends(get_current_user),
           db: Session = Depends(get_db)) -> dict:
    """Подтверждение личности для важных операций: пароль и код из приложения. Неверные — как неудачный вход (лимит попыток)."""
    _guarded(request, user.username,
             lambda: AuthService(db).reauthenticate(user.id, user.session_id, body.password, body.code,
                                                    get_settings().auth_secret_key))
    return {"status": "confirmed", "valid_minutes": get_settings().auth_reauth_minutes}


@router.post("/auth/password", response_model=TokenResponse)
def change_password(body: PasswordChange, request: Request, user: CurrentUser = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> TokenResponse:
    """Смена пароля самим пользователем (документ 05, §12.11): текущий пароль и, если включён второй фактор, код из приложения.
    Все сеансы, включая этот, завершаются одной транзакцией с новым паролем; этому устройству выдаётся новый сеанс (в ответе).
    Неверный пароль или код — неудачная попытка входа (общее ограничение попыток)."""
    service = AuthService(db)
    listener = request_listener(request)
    ttl = _ttl_minutes(user.role, listener)
    token, expires_at, _mfa = _guarded(request, user.username, lambda: service.change_own_password(
        user.id, body.current_password, body.new_password, body.code, get_settings().auth_secret_key,
        ttl_minutes=ttl, context=_login_context(request), listener=listener))
    logger.info("Пользователь %s сменил свой пароль; прежние сеансы завершены", user.username)
    seconds = max(60, int((expires_at - utcnow()).total_seconds()))
    return TokenResponse(access_token=token, expires_in=seconds, username=user.username, role=user.role)


@router.post("/auth/password/initial", response_model=TokenResponse | PasswordSetResponse)
def set_initial_password(body: InitialPassword, request: Request, user: CurrentUser = Depends(get_password_changing_user),
                         db: Session = Depends(get_db)) -> TokenResponse | PasswordSetResponse:
    """Установка своего пароля вместо временного (документ 05, §12.3) — только с пропуском «смена пароля». Все сеансы и пропуски
    завершаются. Дальше — как после верного пароля при входе: если нужен второй фактор (или его настройка), сеанс здесь НЕ
    выдаётся — войти заново с новым паролем; иначе выдаётся обычный сеанс."""
    service = AuthService(db)
    settings = get_settings()
    updated = _guarded(request, user.username,
                       lambda: service.set_initial_password(user.id, body.new_password, body.code, settings.auth_secret_key))
    logger.info("Пользователь %s установил свой пароль вместо временного", user.username)
    role = parse_role(updated.role)
    decision = after_password(totp_enabled=service.totp_enabled(updated.id), is_admin=role == Role.ADMIN,
                              require_for_admins=settings.auth_require_2fa_for_admins, listener=request_listener(request))
    if decision != ISSUE or role is None:
        return PasswordSetResponse()
    return _issue(service, updated, role, request, mfa=False)


@router.post("/auth/logout")
def logout(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> dict:
    # Идемпотентно: повторный logout с уже отозванным токеном тоже отвечает ok.
    AuthService(db).revoke_token(token)
    return {"status": "logged_out"}


@router.post("/auth/logout-all")
def logout_all(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """Выйти на всех устройствах, включая это: новое поколение сеансов и удаление токенов — одной транзакцией."""
    AuthService(db).revoke_all_tokens(user.id)
    return {"status": "logged_out_everywhere"}


@router.get("/auth/me", response_model=MeResponse)
def me(request: Request, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> MeResponse:
    """Права в ответе — действующие здесь (В-5): вне административного входа права «только администратор» (пользователи,
    магазины и полки, камеры, правка каталога) не перечисляются — страницы прячут эти функции. Решает всё равно сервер:
    каждый такой маршрут проверяет вход сам (deps.require_permission)."""
    admin_here = admin_rights_apply(request_listener(request), user.session_listener)
    role_perms = sorted(ROLE_PERMISSIONS[user.role], key=lambda p: p.value)
    return MeResponse(
        username=user.username,
        role=user.role,
        all_stores=user.role in ALL_STORES_ROLES,
        store_ids=sorted(user.store_ids),
        permissions=[p for p in role_perms if admin_here or p not in ADMIN_ONLY_PERMISSIONS],
        role_permissions=role_perms,
        mfa_enabled=AuthService(db).totp_enabled(user.id),
        is_owner=user.is_owner,
        admin_listener=admin_here,
        language=AuthService(db).get_user(user.id).language,
    )


@router.put("/auth/me/language")
def set_my_language(body: LanguageIn, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """Запомнить язык интерфейса в профиле: при входе с другого устройства страница откроется на нём. null — забыть выбор."""
    if body.language is not None and body.language not in LANGUAGES:
        raise ApiError(422, "language_unknown", "Такого языка в программе нет",
                       {"allowed": list(LANGUAGES)})
    return {"language": AuthService(db).set_language(user.id, body.language)}


# ------------------------------------------------------------------- мои сеансы


@router.get("/auth/sessions", response_model=list[SessionOut])
def my_sessions(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> list[SessionOut]:
    return [SessionOut(**s) for s in AuthService(db).list_sessions(user.id, current_session_id=user.session_id)]


@router.delete("/auth/sessions/{session_id}")
def end_my_session(session_id: str, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    try:
        AuthService(db).revoke_session(user.id, session_id)
    except AuthServiceError as exc:
        raise from_app_error(exc) from exc
    return {"status": "ended", "current": session_id == user.session_id}


@router.post("/auth/sessions/revoke-others")
def end_other_sessions(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return {"status": "ended", "count": AuthService(db).revoke_other_sessions(user.id, user.session_id)}


# --------------------------------------------------------------- второй фактор


def _required_for(user: CurrentUser) -> bool:
    return user.role == Role.ADMIN and get_settings().auth_require_2fa_for_admins


@router.get("/auth/2fa")
def mfa_status(user: CurrentUser = Depends(get_enrolling_user), db: Session = Depends(get_db)) -> dict:
    status_ = AuthService(db).totp_status(user.id, get_settings().auth_secret_key, required=_required_for(user))
    status_["enrolling"] = user.session_scope == SCOPE_ENROLL
    return status_


@router.post("/auth/2fa/setup")
def mfa_setup(body: PasswordConfirm, request: Request, user: CurrentUser = Depends(get_enrolling_user),
              db: Session = Depends(get_db)) -> dict:
    """Секрет показывается ОДИН раз: его вводят в приложение-аутентификатор (или открывают ссылку otpauth:// на телефоне).
    Пропуск настройки (администратор без 2FA) — только на административном входе и без повторного пароля. Неверный пароль
    учитывается в ограничении попыток."""
    enrolling = user.session_scope == SCOPE_ENROLL
    if enrolling and request_listener(request) != LISTENER_ADMIN:
        raise admin_listener_error()
    return _guarded(request, user.username,
                    lambda: AuthService(db).begin_totp_setup(user.id, body.password, get_settings().auth_secret_key,
                                                             password_verified=enrolling))


@router.post("/auth/2fa/confirm")
def mfa_confirm(body: CodeConfirm, request: Request, user: CurrentUser = Depends(get_enrolling_user),
                db: Session = Depends(get_db)) -> dict:
    """Включить второй фактор. Пропуск настройки меняется на обычный сеанс (со вторым фактором) — в ответе access_token.
    Коды восстановления — в ответе, ОДИН раз."""
    enrolling = user.session_scope == SCOPE_ENROLL
    if enrolling and request_listener(request) != LISTENER_ADMIN:
        raise admin_listener_error()
    service = AuthService(db)
    result = _guarded(request, user.username,
                      lambda: service.confirm_totp_setup(user.id, body.code, get_settings().auth_secret_key,
                                                         keep_session_id=None if enrolling else user.session_id))
    answer = {"status": "enabled", "other_sessions_ended": result["ended"] - (1 if enrolling else 0),
              "recovery_codes": result["recovery_codes"]}
    if enrolling:
        issued = _issue(service, service.get_user(user.id), user.role, request, mfa=True)
        answer.update(access_token=issued.access_token, expires_in=issued.expires_in, username=issued.username, role=issued.role)
    return answer


@router.post("/auth/2fa/disable")
def mfa_disable(body: PasswordAndCode, request: Request, user: CurrentUser = Depends(get_current_user),
                db: Session = Depends(get_db)) -> dict:
    _guarded(request, user.username,
             lambda: AuthService(db).disable_totp(user.id, body.password, body.code, get_settings().auth_secret_key,
                                                  forbidden_for_admins=get_settings().auth_require_2fa_for_admins))
    return {"status": "disabled"}


@router.post("/auth/2fa/recovery-codes")
def mfa_new_recovery_codes(body: PasswordAndCode, request: Request, user: CurrentUser = Depends(get_current_user),
                           db: Session = Depends(get_db)) -> dict:
    """Новые коды восстановления (старые перестают действовать): пароль и свежий код. Коды — в ответе ОДИН раз."""
    codes = _guarded(request, user.username,
                     lambda: AuthService(db).regenerate_recovery_codes(user.id, body.password, body.code,
                                                                       get_settings().auth_secret_key))
    return {"recovery_codes": codes}


# ------------------------------------------------------------------ пользователи


def _guard_target(service: AuthService, actor: CurrentUser, target_id: str) -> None:
    """Владельца меняет только сам владелец (документ 05, §8)."""
    refusal = owner_guard(actor_is_owner=actor.is_owner, target_is_owner=service.is_owner(target_id), actor_id=actor.id,
                          target_id=target_id)
    if refusal:
        raise ApiError(status.HTTP_403_FORBIDDEN, refusal, "Учётной записью владельца управляет только сам владелец")


def _guard_admin_grant(service: AuthService, actor: CurrentUser) -> None:
    """Роль администратора выдаёт только владелец: захваченная учётная запись обычного администратора не может завести себе
    запасных администраторов. Пока владелец не назначен (старые установки), правило не действует — назначьте его с консоли."""
    if not actor.is_owner and service.owner_id() is not None:
        raise ApiError(status.HTTP_403_FORBIDDEN, "owner_only", "Назначить администратора может только владелец системы")


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    admin: CurrentUser = Depends(require_recent_auth(Permission.USER_MANAGE)),
) -> UserOut:
    """Новый сотрудник всегда начинает с временного пароля (документ 05, §12.1–§12.2)."""
    service = AuthService(db)
    if body.role == Role.ADMIN:
        _guard_admin_grant(service, admin)
    generated = body.password is None
    initial = body.password if body.password is not None else temp_password.generate()
    try:
        user = service.create_user(body.username, initial, body.role, body.store_ids)
        value, _deadline = service.set_temporary_password(user.id, initial, hours=get_settings().auth_temp_password_hours)
    except (PolicyViolation, UserAlreadyExistsError, UnknownStoreError) as exc:
        raise from_app_error(exc) from exc          # статус по виду ошибки: 422 / 409 / 404, как раньше
    out = _user_out(user, service)
    if generated:
        out.temporary_password = value
    return out


@router.get("/users", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission(Permission.USER_MANAGE)),
) -> list[UserOut]:
    service = AuthService(db)
    return [_user_out(u, service) for u in service.list_users()]


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    body: UserUpdate,
    db: Session = Depends(get_db),
    admin: CurrentUser = Depends(require_recent_auth(Permission.USER_MANAGE)),
) -> UserOut:
    service = AuthService(db)
    _guard_target(service, admin, user_id)
    if body.role == Role.ADMIN:
        try:
            current_role = parse_role(service.get_user(user_id).role)
        except UserNotFoundError as exc:
            raise from_app_error(exc) from exc
        if current_role != Role.ADMIN:
            _guard_admin_grant(service, admin)
    # Защита от самоблокировки: админ не может отключить или разжаловать самого себя.
    # Вместе с этим правилом в системе всегда остаётся хотя бы один активный админ.
    if user_id == admin.id and (
        body.is_active is False or (body.role is not None and body.role != Role.ADMIN)
    ):
        raise ApiError(400, "self_lockout",
                       "Нельзя отключить себя или понизить собственную роль — попросите другого администратора")
    if user_id == admin.id and body.password is not None:
        # Свой пароль — только «Настройки» → «Сменить пароль» (текущий пароль и код), не как временный пароль для себя.
        raise ApiError(400, "own_password_in_settings", "Свой пароль меняется в «Настройках» → «Безопасность входа»")

    try:
        if body.password is not None:
            check_password_policy(body.password)  # сначала проверка: при плохом пароле ничего не применяется
        service.update_user(
            user_id,
            role=body.role,
            is_active=body.is_active,
            store_ids=body.store_ids,
        )
        if body.password is not None:            # пароль, заданный администратором, — временный (документ 05, §12.4)
            service.set_temporary_password(user_id, body.password, hours=get_settings().auth_temp_password_hours)
    except (UserNotFoundError, UnknownStoreError, PolicyViolation) as exc:
        raise from_app_error(exc) from exc          # 404 / 404 / 422, как раньше
    return _user_out(service.get_user(user_id), service)


@router.post("/users/{user_id}/temporary-password", response_model=TemporaryPasswordOut)
def issue_temporary_password(
    user_id: str,
    db: Session = Depends(get_db),
    admin: CurrentUser = Depends(require_recent_auth(Permission.USER_MANAGE)),
) -> TemporaryPasswordOut:
    """«Выдать новый временный пароль» (документ 05, §12.4): сервер генерирует, старый пароль перестаёт действовать сразу, все
    сеансы сотрудника завершаются. Пароль — в ответе ОДИН раз. Владельцу — только он сам с консоли; себе — нельзя."""
    service = AuthService(db)
    try:
        target = service.get_user(user_id)
    except UserNotFoundError as exc:
        raise from_app_error(exc) from exc
    _guard_target(service, admin, user_id)
    if user_id == admin.id:
        raise ApiError(400, "own_password_in_settings", "Свой пароль меняется в «Настройках» → «Безопасность входа»")
    if parse_role(target.role) == Role.ADMIN and not admin.is_owner:
        raise ApiError(status.HTTP_403_FORBIDDEN, "owner_only", "Это может сделать только владелец системы")
    value, deadline = service.set_temporary_password(user_id, hours=get_settings().auth_temp_password_hours)
    logger.warning("Пользователю %s выдан новый временный пароль (выдал %s); его сеансы завершены", target.username, admin.username)
    return TemporaryPasswordOut(temporary_password=value, expires_at=deadline)


@router.get("/users/{user_id}/sessions", response_model=list[SessionOut])
def user_sessions(
    user_id: str,
    db: Session = Depends(get_db),
    admin: CurrentUser = Depends(require_permission(Permission.USER_MANAGE)),
) -> list[SessionOut]:
    service = AuthService(db)
    try:
        service.get_user(user_id)
    except UserNotFoundError as exc:
        raise from_app_error(exc) from exc
    _guard_target(service, admin, user_id)          # сеансы владельца видит только он сам
    return [SessionOut(**s) for s in service.list_sessions(user_id, current_session_id=admin.session_id)]


@router.post("/users/{user_id}/sessions/revoke-all")
def end_user_sessions(
    user_id: str,
    db: Session = Depends(get_db),
    admin: CurrentUser = Depends(require_permission(Permission.USER_MANAGE)),
) -> dict:
    """Администратор завершает все сеансы пользователя (потерян телефон, подозрение на кражу пароля)."""
    service = AuthService(db)
    try:
        service.get_user(user_id)
    except UserNotFoundError as exc:
        raise from_app_error(exc) from exc
    _guard_target(service, admin, user_id)
    service.revoke_all_tokens(user_id)
    return {"status": "ended"}


@router.post("/users/{user_id}/2fa/reset")
def reset_user_2fa(
    user_id: str,
    db: Session = Depends(get_db),
    admin: CurrentUser = Depends(require_recent_auth(Permission.USER_MANAGE)),
) -> dict:
    """Восстановление доступа при потере телефона (документ 18, §4.1): второй фактор сотрудника сбрасывает администратор,
    администратора — только владелец; свой — нельзя (коды восстановления или консоль сервера). Все сеансы пользователя
    завершаются; администратор при следующем входе получит пропуск настройки (только на административном входе)."""
    service = AuthService(db)
    try:
        target = service.get_user(user_id)
    except UserNotFoundError as exc:
        raise from_app_error(exc) from exc
    if user_id == admin.id:
        raise ApiError(status.HTTP_403_FORBIDDEN, "mfa_reset_self",
                       "Свой второй фактор так не сбрасывается: войдите по коду восстановления или обратитесь к владельцу")
    _guard_target(service, admin, user_id)
    if parse_role(target.role) == Role.ADMIN and not admin.is_owner:
        raise ApiError(status.HTTP_403_FORBIDDEN, "owner_only", "Второй фактор администратора сбрасывает только владелец")
    service.reset_totp(user_id)
    logger.warning("Второй фактор пользователя %s сброшен администратором %s", target.username, admin.username)
    return {"status": "reset"}
