"""
Сервис аутентификации и управления пользователями (F13); сеансы и второй фактор (2026-11-08, документ 18).

Все РЕШЕНИЯ (проверка пароля, права, скоуп, срок токена, коды TOTP, поколение сеансов, что делать после пароля) живут
в чистых модулях app/core/security.py, totp.py, session_info.py, mfa_policy.py, auth_secrets.py и покрыты тестами без БД;
этот файл — только работа с БД поверх них, поэтому он намеренно тонкий.

Единая система входа: токен — строка api_tokens (в базе только SHA-256), сведения о сеансе — auth_session_meta (один к одному),
поколение сеансов и второй фактор — user_security. Отдельной «удалённой» авторизации нет: вход с телефона, из дома и с
компьютера магазина проходит один и тот же путь.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import account_status, auth_secrets, recovery_codes, temp_password, totp
from app.services import audit
from app.services.audit import AuditActor
from app.core.app_errors import AppError
from app.core.mfa_policy import SCOPE_FULL, renewed_expiry, session_accepted
from app.core.session_info import (
    classify_source,
    clean_device_label,
    epoch_matches,
    ip_fingerprint,
    needs_last_used_update,
    summarize_user_agent,
)
from app.core.security import (
    CurrentUser,
    PolicyViolation,
    Role,
    canonical_username,
    check_password_policy,
    dummy_password_hash,
    generate_token,
    hash_password,
    hash_token,
    parse_role,
    token_is_expired,
    utcnow,
    validate_username,
    verify_password,
)
from app.db.models import (
    ApiToken,
    AuthMfaChallenge,
    AuthRecoveryCode,
    AuthSessionMeta,
    Store,
    User,
    UserSecurity,
    UserStoreAccess,
)


class AuthServiceError(AppError):
    """Ошибки управления пользователями (единая система ошибок, app/core/app_errors.py)."""


class UserAlreadyExistsError(AuthServiceError):
    kind = "conflict"
    default_code = "user_exists"


class UserNotFoundError(AuthServiceError):
    kind = "not_found"
    default_code = "user_not_found"


class UnknownStoreError(AuthServiceError):
    """В store_ids передан идентификатор магазина, которого нет в БД."""

    kind = "not_found"
    default_code = "stores_not_found"


class SessionNotFoundError(AuthServiceError):
    kind = "not_found"
    default_code = "session_not_found"


class WrongPasswordError(AuthServiceError):
    """Повторный ввод пароля для опасного действия (включить/выключить второй фактор) не совпал."""

    kind = "invalid"
    default_code = "password_wrong"


class TempPasswordError(AuthServiceError):
    """Временный пароль: истёк (после ВЕРНОГО пароля — документ 05, §12.2) или его нет."""

    kind = "forbidden"
    default_code = "temp_password_expired"


class MfaStateError(AuthServiceError):
    """Действие со вторым фактором не подходит к его состоянию (уже включён, не начата настройка, не включён)."""

    kind = "conflict"
    default_code = "mfa_state"


class MfaCodeError(AuthServiceError):
    """Код из приложения неверный, устарел или уже использован. Пропуск второго шага ещё действует (params.attempts_left)."""

    kind = "invalid"
    default_code = "mfa_code_wrong"


class MfaChallengeError(AuthServiceError):
    """Пропуск второго шага не найден, истёк, израсходован или исчерпаны попытки — войти заново с паролем."""

    kind = "unauthorized"
    default_code = "mfa_expired"


class MfaKeyError(AuthServiceError):
    """На сервере не задан или не подходит AUTH_SECRET_KEY — второй фактор недоступен. Текст безопасен для показа."""

    kind = "unavailable"
    default_code = "mfa_unavailable"


class UserArchivedError(AuthServiceError):
    """Учётная запись в архиве (доступ отозван, документ 05, §12.7): её нельзя включить или изменить, логин не используется снова."""

    kind = "conflict"
    default_code = "user_archived"


class AccessExpiredError(AuthServiceError):
    """Наступила дата «доступ до» (документ 05, §12.1). Сообщается только после ВЕРНОГО пароля, как истёкший временный пароль."""

    kind = "forbidden"
    default_code = "access_expired"


class OwnerError(AuthServiceError):
    """Действие с владельцем системы запрещено (назначение — только с консоли сервера)."""

    kind = "forbidden"
    default_code = "owner_protected"


@dataclass(frozen=True)
class MfaResult:
    """Итог второго шага входа: пользователь и был ли использован код восстановления (сколько их осталось)."""

    user: User
    recovery_used: bool = False
    recovery_left: int | None = None


@dataclass(frozen=True)
class LoginContext:
    """Откуда пришёл вход: для списка сеансов (без полного IP и полной строки браузера, app/core/session_info.py)."""

    ip: str | None = None
    user_agent: str | None = None
    device_label: str | None = None

    @property
    def source(self) -> str:
        return classify_source(self.ip)


class AuthService:
    def __init__(self, db: Session, actor: AuditActor | None = None):
        self.db = db
        # Кто действует — для журнала действий (документ 05, §16). Не задан — «система»; маршруты передают вошедшего сотрудника,
        # консольные утилиты — audit.CONSOLE.
        self.actor = actor

    def _audit(self, action: str, target_id: str | None, *, before: dict | None = None, after: dict | None = None,
               reason: str | None = None, target_type: str = "user", actor: AuditActor | None = None) -> None:
        """Запись в журнал в ТЕКУЩЕЙ транзакции (до commit вызывающего метода): действие и запись сохраняются вместе."""
        audit.record(self.db, action, actor=actor or self.actor, target_type=target_type, target_id=target_id,
                     before=before, after=after, reason=reason)

    def _profile(self, user: User) -> dict:
        return {"role": user.role, "is_active": bool(user.is_active), "access_until": user.access_until,
                "store_ids": sorted(a.store_id for a in user.store_access)}

    # ------------------------------------------------------------------ вход

    def authenticate(self, username: str, password: str) -> User | None:
        """Возвращает пользователя при верной паре логин/пароль и активной учётке, иначе None.

        Проверка пароля выполняется ВСЕГДА (для несуществующего логина — против
        хеша-пустышки), чтобы время ответа не выдавало, существует ли логин.
        """
        canon = canonical_username(username)
        user = None
        if canon:
            user = self.db.execute(select(User).where(User.username == canon)).scalar_one_or_none()

        stored_hash = user.password_hash if user is not None else dummy_password_hash()
        password_ok = verify_password(password, stored_hash)

        if user is None or not password_ok or not user.is_active:
            return None
        return user

    def issue_token(self, user: User, ttl_minutes: int, *, context: LoginContext | None = None,
                    mfa: bool = False, secret_key: str = "", scope: str = SCOPE_FULL, listener: str = "all",
                    auth_at: datetime | None = None) -> tuple[str, datetime]:
        """Выдаёт новый токен. Сырое значение возвращается ОДИН раз — в БД остаётся только хеш.
        Сведения о сеансе (устройство, откуда, поколение, пройден ли второй фактор, область, вход программы, время проверки
        пароля) пишутся в той же транзакции. Новый сеанс = только что подтверждённая личность (reauth_at = сейчас)."""
        result = self._add_session(user, ttl_minutes, context=context, mfa=mfa, secret_key=secret_key, scope=scope,
                                   listener=listener, auth_at=auth_at)
        if scope == SCOPE_FULL:
            ctx = context or LoginContext()
            device = summarize_user_agent(ctx.user_agent)
            self._audit("login.succeeded", user.id, after={"device": device["kind"], "os": device["os"], "source": ctx.source,
                                                           "mfa": mfa, "listener": listener},
                        actor=AuditActor(user.id, (self.actor.via if self.actor else "api"), self.actor.ip_hash if self.actor else None))
        self.db.commit()
        return result

    def _add_session(self, user: User, ttl_minutes: int, *, context: LoginContext | None, mfa: bool, secret_key: str,
                     scope: str, listener: str, auth_at: datetime | None) -> tuple[str, datetime]:
        """issue_token без commit: вызывающий фиксирует вместе со своим изменением (смена пароля — документ 05, §12.11, п. 4)."""
        now = utcnow()
        # Попутно чистим просроченные токены этого пользователя, чтобы таблица не росла вечно.
        self.db.execute(delete(ApiToken).where(ApiToken.user_id == user.id, ApiToken.expires_at <= now))

        raw_token = generate_token()
        expires_at = now + timedelta(minutes=ttl_minutes)
        token = ApiToken(token_hash=hash_token(raw_token), user_id=user.id, expires_at=expires_at)
        self.db.add(token)
        self.db.flush()                                   # id токена нужен для строки сведений о сеансе
        ctx = context or LoginContext()
        device = summarize_user_agent(ctx.user_agent)
        security = self._security(user.id)
        self.db.add(AuthSessionMeta(
            token_id=token.id, user_id=user.id, session_epoch=security.session_epoch if security else 0,
            device_label=clean_device_label(ctx.device_label), device_kind=device["kind"], device_os=device["os"],
            device_browser=device["browser"], source=ctx.source,
            ip_hash=ip_fingerprint(ctx.ip, auth_secrets.ip_hmac_key(secret_key)), mfa=mfa, scope=scope, listener=listener,
            auth_at=auth_at or now, reauth_at=now if scope == SCOPE_FULL else None, created_at=now, last_used_at=now,
        ))
        return raw_token, expires_at

    def resolve_token(self, raw_token: str, *, require_admin_mfa: bool = False,
                      allow_scopes: tuple[str, ...] = (SCOPE_FULL,)) -> CurrentUser | None:
        """Токен -> CurrentUser, либо None (не найден / просрочен / учётка отключена / роль неизвестна / чужое поколение /
        область не подходит / сеанс администратора без второго фактора при require_admin_mfa — app/core/mfa_policy.py)."""
        if not raw_token:
            return None

        row = self.db.execute(
            select(ApiToken).where(ApiToken.token_hash == hash_token(raw_token))
        ).scalar_one_or_none()
        if row is None or token_is_expired(row.expires_at):
            return None

        user = self.db.get(User, row.user_id)
        if user is None or not account_status.can_sign_in(is_active=user.is_active, archived_at=user.archived_at,
                                                          access_until=user.access_until, now=utcnow()):
            return None                       # отключён, в архиве или наступил «доступ до» — со следующего же запроса

        role = parse_role(user.role)
        if role is None:
            return None

        # Поколение сеансов (документ 05, §12.11): «выйти везде», смена пароля и отключение увеличивают поколение пользователя —
        # токен прежнего поколения не действует, даже если его строку по какой-то причине не удалили.
        meta = self.db.get(AuthSessionMeta, row.id)
        security = self._security(user.id)
        if not epoch_matches(meta.session_epoch if meta else None, security.session_epoch if security else None):
            return None
        scope = meta.scope if meta else SCOPE_FULL                # токены до 2026-11-08: обычные сеансы без второго фактора
        mfa = bool(meta.mfa) if meta else False
        if scope not in allow_scopes:
            return None
        if scope == SCOPE_FULL and not session_accepted(is_admin=role == Role.ADMIN, require_for_admins=require_admin_mfa,
                                                        scope=scope, mfa=mfa):
            return None
        if scope == SCOPE_FULL and security is not None and security.must_change_password:
            return None                       # при временном пароле обычных сеансов нет (документ 05, §12.2), где бы токен ни взялся
        if meta is not None and needs_last_used_update(meta.last_used_at, utcnow()):
            meta.last_used_at = utcnow()
            self.db.commit()

        return CurrentUser(
            id=user.id,
            username=user.username,
            role=role,
            store_ids=frozenset(a.store_id for a in user.store_access),
            session_id=row.id,
            session_scope=scope,
            mfa=mfa,
            reauth_at=meta.reauth_at if meta else None,
            is_owner=bool(security and security.is_owner),
            session_listener=meta.listener if meta else None,
        )

    def change_own_password(self, user_id: str, current_password: str, new_password: str, code: str | None, secret_key: str, *,
                            ttl_minutes: int, context: LoginContext | None, listener: str) -> tuple[str, datetime, bool]:
        """Смена пароля самим пользователем (документ 05, §12.11): нужен текущий пароль и, если включён второй фактор, свежий код
        (повтор запрещён; коды восстановления не принимаются). ОДНОЙ транзакцией: новый хеш, новое поколение сеансов (все токены,
        включая текущий, перестают действовать) и новый сеанс этого устройства уже нового поколения. -> (токен, срок, mfa)."""
        user = self.get_user(user_id)
        self._require_password(user, current_password)
        check_password_policy(new_password)
        if verify_password(new_password, user.password_hash):
            raise PolicyViolation("Новый пароль совпадает с прежним", "password_same")
        security = self._security(user_id, lock=True)
        mfa = bool(security is not None and security.totp_secret_enc and security.totp_enabled_at)
        if mfa:
            step = totp.verify(self._decrypt(security.totp_secret_enc, secret_key), code or "", not_before=security.totp_last_step)
            if step is None:
                raise MfaCodeError("Код не подошёл или уже был использован — дождитесь следующего кода в приложении")
            security.totp_last_step = step
        user.password_hash = hash_password(new_password)
        security = self._security(user_id, create=True, lock=True)
        security.must_change_password, security.temp_password_expires_at = False, None
        self._end_all_sessions(user_id)
        raw, expires_at = self._add_session(user, ttl_minutes, context=context, mfa=mfa, secret_key=secret_key, scope=SCOPE_FULL,
                                            listener=listener, auth_at=None)
        self._audit("user.password_changed", user_id, after={"mfa": mfa})
        self.db.commit()
        return raw, expires_at, mfa

    # ------------------------------------------------------------- временный пароль (документ 05, §12.2–§12.4)

    def temporary_password_state(self, user_id: str) -> tuple[bool, datetime | None]:
        security = self._security(user_id)
        if security is None or not security.must_change_password:
            return False, None
        return True, security.temp_password_expires_at

    def set_temporary_password(self, user_id: str, password: str | None = None, *, hours: int | None) -> tuple[str, datetime | None]:
        """Выдать временный пароль (сгенерировать или принять заданный — старые клиенты и консоль). ОДНОЙ транзакцией: новый хеш,
        признак «сменить при первом входе», срок, завершение всех сеансов. Прежний пароль перестаёт действовать сразу.
        Возвращает пароль (показать ОДИН раз тому, кто выдал) и срок."""
        user = self.get_user(user_id)
        value = password if password is not None else temp_password.generate()
        check_password_policy(value)
        user.password_hash = hash_password(value)
        security = self._security(user_id, create=True, lock=True)
        security.must_change_password = True
        security.temp_password_expires_at = temp_password.expires_at(utcnow(), hours)
        security.updated_at = utcnow()
        self._end_all_sessions(user_id)
        self._audit("user.temp_password_issued", user_id, after={"expires_at": security.temp_password_expires_at})
        self.db.commit()
        return value, security.temp_password_expires_at

    def set_initial_password(self, user_id: str, new_password: str, code: str | None, secret_key: str) -> User:
        """Установка своего пароля вместо временного (документ 05, §12.3): правила пароля, не совпадает с временным и с логином;
        если включён второй фактор — свежий код (временный пароль сам по себе не даёт сменить пароль администратору со 2FA).
        ОДНОЙ транзакцией: новый хеш (временный перестаёт существовать), признак снят, все сеансы и пропуски завершены."""
        user = self.get_user(user_id)
        security = self._security(user_id, create=True, lock=True)
        if not security.must_change_password:
            raise TempPasswordError("Временного пароля нет — смените пароль в «Настройках»", "password_not_temporary")
        if temp_password.is_expired(security.temp_password_expires_at, utcnow()):
            raise TempPasswordError("Временный пароль истёк. Обратитесь к администратору за новым", "temp_password_expired")
        check_password_policy(new_password)
        if new_password.strip().lower() == user.username.lower():
            raise PolicyViolation("Пароль не должен совпадать с логином", "password_is_login")
        if verify_password(new_password, user.password_hash):
            raise PolicyViolation("Новый пароль совпадает с временным", "password_same")
        if security.totp_secret_enc and security.totp_enabled_at:
            step = totp.verify(self._decrypt(security.totp_secret_enc, secret_key), code or "", not_before=security.totp_last_step)
            if step is None:
                raise MfaCodeError("Код не подошёл или уже был использован — дождитесь следующего кода в приложении")
            security.totp_last_step = step
        user.password_hash = hash_password(new_password)
        security.must_change_password, security.temp_password_expires_at = False, None
        security.updated_at = utcnow()
        self._end_all_sessions(user_id)
        self._audit("user.first_password_set", user_id)
        self.db.commit()
        self.db.refresh(user)
        return user

    def refresh_session(self, session_id: str, ttl_minutes: int, *, max_hours: int | None) -> tuple[str, datetime] | None:
        """Безопасное продление: НОВЫЙ токен вместо старого (старый удаляется в той же транзакции), сведения о сеансе переносятся.
        Предел от проверки пароля (В-4) не сдвигается; достигнут — None (нужно войти заново)."""
        now = utcnow()
        old = self.db.execute(select(ApiToken).where(ApiToken.id == session_id).with_for_update()).scalar_one_or_none()
        meta = self.db.get(AuthSessionMeta, session_id)
        if old is None or token_is_expired(old.expires_at, now) or (meta is not None and meta.scope != SCOPE_FULL):
            return None
        auth_at = (meta.auth_at if meta and meta.auth_at else old.created_at) or now
        expires_at = renewed_expiry(now=now, auth_at=auth_at, ttl_minutes=ttl_minutes, max_hours=max_hours)
        if expires_at is None:
            return None
        raw_token = generate_token()
        token = ApiToken(token_hash=hash_token(raw_token), user_id=old.user_id, expires_at=expires_at)
        self.db.add(token)
        self.db.flush()
        if meta is not None:
            self.db.add(AuthSessionMeta(
                token_id=token.id, user_id=meta.user_id, session_epoch=meta.session_epoch, device_label=meta.device_label,
                device_kind=meta.device_kind, device_os=meta.device_os, device_browser=meta.device_browser, source=meta.source,
                ip_hash=meta.ip_hash, mfa=meta.mfa, scope=meta.scope, listener=meta.listener, auth_at=auth_at,
                reauth_at=meta.reauth_at, created_at=meta.created_at, last_used_at=now,
            ))
        self.db.delete(old)
        self.db.commit()
        return raw_token, expires_at

    def reauthenticate(self, user_id: str, session_id: str, password: str, code: str | None, secret_key: str) -> None:
        """Подтверждение личности для важных операций: пароль и, если второй фактор включён, свежий код (повтор запрещён).
        Коды восстановления здесь не принимаются — они только для входа при потере телефона."""
        user = self.get_user(user_id)
        self._require_password(user, password)
        security = self._security(user_id, lock=True)
        if security is not None and security.totp_secret_enc:
            step = totp.verify(self._decrypt(security.totp_secret_enc, secret_key), code or "", not_before=security.totp_last_step)
            if step is None:
                raise MfaCodeError("Код не подошёл или уже был использован — дождитесь следующего кода в приложении")
            security.totp_last_step = step
        meta = self.db.get(AuthSessionMeta, session_id)
        if meta is None or meta.user_id != user_id:
            raise SessionNotFoundError("Сеанс не найден — войдите заново")
        meta.reauth_at = utcnow()
        self.db.commit()

    # ---------------------------------------------------------------- владелец (документ 05, §8)

    def is_owner(self, user_id: str) -> bool:
        security = self._security(user_id)
        return bool(security and security.is_owner)

    def owner_id(self) -> str | None:
        return self.db.execute(select(UserSecurity.user_id).where(UserSecurity.is_owner.is_(True))).scalars().first()

    def set_owner(self, user_id: str, *, transfer: bool = False) -> None:
        """Только с консоли сервера (scripts.bootstrap, scripts.user_admin owner): назначить владельца или передать права.
        Владелец — администратор. Второй владелец не появляется: передача снимает признак с прежнего в той же транзакции."""
        user = self.get_user(user_id)
        if parse_role(user.role) != Role.ADMIN or not user.is_active:
            raise OwnerError("Владельцем может быть только действующий администратор", "owner_must_be_admin")
        current = self.db.execute(select(UserSecurity).where(UserSecurity.is_owner.is_(True)).with_for_update()).scalars().all()
        if current and not transfer and all(s.user_id != user_id for s in current):
            raise OwnerError("Владелец уже назначен — передать права можно только явно (--transfer)", "owner_exists")
        for security in current:
            security.is_owner = False
        target = self._security(user_id, create=True, lock=True)
        target.is_owner = True
        target.updated_at = utcnow()
        self._audit("owner.granted", user_id, before={"owners": [x.user_id for x in current]}, after={"owner": user_id})
        self.db.commit()

    def revoke_token(self, raw_token: str) -> bool:
        result = self.db.execute(delete(ApiToken).where(ApiToken.token_hash == hash_token(raw_token)))
        self.db.commit()
        return bool(result.rowcount)

    def revoke_all_tokens(self, user_id: str) -> None:
        """«Выйти везде»: новое поколение сеансов и удаление всех токенов — одной транзакцией (документ 05, §12.11)."""
        self._end_all_sessions(user_id)
        self._audit("session.revoked_all", user_id)
        self.db.commit()

    def _end_all_sessions(self, user_id: str) -> None:
        """Без commit: вызывающий фиксирует вместе со своим изменением (пароль, отключение) — либо всё, либо ничего."""
        security = self._security(user_id, create=True, lock=True)
        security.session_epoch = int(security.session_epoch or 0) + 1
        security.updated_at = utcnow()
        self.db.execute(delete(ApiToken).where(ApiToken.user_id == user_id))

    # ---------------------------------------------------------------- сеансы

    def list_sessions(self, user_id: str, *, current_session_id: str | None = None) -> list[dict]:
        """Действующие сеансы пользователя, новые сверху. Без полного IP и без самих токенов."""
        now = utcnow()
        rows = self.db.execute(
            select(ApiToken, AuthSessionMeta).outerjoin(AuthSessionMeta, AuthSessionMeta.token_id == ApiToken.id)
            .where(ApiToken.user_id == user_id, ApiToken.expires_at > now)
            .order_by(ApiToken.created_at.desc())
        ).all()
        security = self._security(user_id)
        epoch = security.session_epoch if security else 0
        sessions = []
        for token, meta in rows:
            if not epoch_matches(meta.session_epoch if meta else None, epoch):
                continue                                      # недействующее поколение: в списке не показывается
            sessions.append({
                "id": token.id, "created_at": token.created_at, "expires_at": token.expires_at,
                "last_used_at": meta.last_used_at if meta else None,
                "device_label": meta.device_label if meta else None,
                "device_kind": meta.device_kind if meta else "unknown",
                "device_os": meta.device_os if meta else "", "device_browser": meta.device_browser if meta else "",
                "source": meta.source if meta else "unknown", "mfa": bool(meta.mfa) if meta else False,
                "current": token.id == current_session_id,
            })
        return sessions

    def revoke_session(self, user_id: str, session_id: str) -> None:
        """Завершить один свой сеанс (или сеанс пользователя — для администратора). Чужой id — «не найден», а не «запрещено»."""
        result = self.db.execute(delete(ApiToken).where(ApiToken.id == session_id, ApiToken.user_id == user_id))
        if result.rowcount:
            self._audit("session.revoked", user_id, after={"session_id": session_id})
        self.db.commit()
        if not result.rowcount:
            raise SessionNotFoundError("Сеанс не найден или уже завершён")

    def revoke_other_sessions(self, user_id: str, current_session_id: str | None) -> int:
        """Завершить все сеансы, кроме текущего. Возвращает, сколько завершено."""
        query = delete(ApiToken).where(ApiToken.user_id == user_id)
        if current_session_id:
            query = query.where(ApiToken.id != current_session_id)
        result = self.db.execute(query)
        self._audit("session.revoked_others", user_id, after={"kept_session_id": current_session_id})
        self.db.commit()
        return int(result.rowcount or 0)

    # ------------------------------------------------------ второй фактор (TOTP)

    def totp_enabled(self, user_id: str) -> bool:
        security = self._security(user_id)
        return bool(security and security.totp_secret_enc and security.totp_enabled_at)

    def totp_status(self, user_id: str, secret_key: str, *, required: bool = False) -> dict:
        security = self._security(user_id)
        return {"enabled": self.totp_enabled(user_id), "pending": bool(security and security.totp_pending_enc),
                "enabled_at": security.totp_enabled_at if security else None,
                "available": auth_secrets.is_configured(secret_key), "required": required,
                "recovery_left": self.recovery_codes_left(user_id)}

    def begin_totp_setup(self, user_id: str, password: str | None, secret_key: str, *, password_verified: bool = False) -> dict:
        """Шаг 1 настройки: проверка пароля, новый секрет (хранится зашифрованным как «ожидающий»). Секрет возвращается ОДИН раз.
        password_verified — пропуск настройки: пароль проверен при входе минуты назад (пропуск живёт 10 минут)."""
        user = self.get_user(user_id)
        if not password_verified:
            self._require_password(user, password or "")
        if self.totp_enabled(user_id):
            raise MfaStateError("Второй фактор уже включён. Чтобы привязать другое приложение, сначала выключите его",
                                "mfa_already_enabled")
        secret = totp.generate_secret()
        security = self._security(user_id, create=True, lock=True)
        security.totp_pending_enc = self._encrypt(secret, secret_key)
        security.updated_at = utcnow()
        self.db.commit()
        return {"secret": secret, "secret_grouped": totp.group_secret(secret),
                "otpauth_uri": totp.provisioning_uri(secret, user.username)}

    def confirm_totp_setup(self, user_id: str, code: str, secret_key: str, *, keep_session_id: str | None = None) -> dict:
        """Шаг 2: код из приложения подтверждает, что секрет сохранён верно. Включает второй фактор, создаёт коды восстановления
        (возвращаются ОДИН раз) и завершает остальные сеансы (они открыты без второго фактора; keep_session_id=None — все,
        включая пропуск настройки). -> {"ended": n, "recovery_codes": [...]}"""
        security = self._security(user_id, lock=True)
        if security is None or not security.totp_pending_enc:
            raise MfaStateError("Настройка второго фактора не начата или уже завершена — начните заново", "mfa_setup_not_started")
        secret = self._decrypt(security.totp_pending_enc, secret_key)
        step = totp.verify(secret, code)
        if step is None:
            raise MfaCodeError("Код не подошёл. Проверьте время на телефоне и введите код, который сейчас показывает приложение")
        security.totp_secret_enc, security.totp_pending_enc = security.totp_pending_enc, None
        security.totp_enabled_at, security.totp_last_step, security.updated_at = utcnow(), step, utcnow()
        query = delete(ApiToken).where(ApiToken.user_id == user_id)
        if keep_session_id:
            query = query.where(ApiToken.id != keep_session_id)
            self.db.execute(
                AuthSessionMeta.__table__.update().where(AuthSessionMeta.token_id == keep_session_id).values(mfa=True))
        ended = int(self.db.execute(query).rowcount or 0)
        codes = self._replace_recovery_codes(user_id, secret_key)
        self._audit("user.2fa_enabled", user_id, after={"sessions_ended": ended})
        self.db.commit()
        return {"ended": ended, "recovery_codes": codes}

    def disable_totp(self, user_id: str, password: str, code: str, secret_key: str, *, forbidden_for_admins: bool = False) -> None:
        """Выключить второй фактор: нужны и пароль, и действующий код (украденного токена сеанса недостаточно).
        forbidden_for_admins — решение В-3: администратору второй фактор выключать нельзя (только сменить телефон — сброс и
        повторная настройка)."""
        user = self.get_user(user_id)
        if forbidden_for_admins and parse_role(user.role) == Role.ADMIN:
            raise MfaStateError("Администратору второй фактор обязателен — его нельзя выключить", "mfa_required_for_admins")
        self._require_password(user, password)
        security = self._security(user_id, lock=True)
        if security is None or not security.totp_secret_enc:
            raise MfaStateError("Второй фактор не включён", "mfa_not_enabled")
        step = totp.verify(self._decrypt(security.totp_secret_enc, secret_key), code, not_before=security.totp_last_step)
        if step is None:
            raise MfaCodeError("Код не подошёл или уже был использован — дождитесь следующего кода в приложении")
        security.totp_secret_enc = security.totp_pending_enc = None
        security.totp_enabled_at, security.totp_last_step, security.updated_at = None, None, utcnow()
        self.db.execute(delete(AuthRecoveryCode).where(AuthRecoveryCode.user_id == user_id))
        self._audit("user.2fa_disabled", user_id)
        self.db.commit()

    def reset_totp(self, user_id: str) -> None:
        """Сброс второго фактора с консоли сервера (scripts.user_admin reset-2fa): потерян телефон. Все сеансы завершаются."""
        self.get_user(user_id)
        security = self._security(user_id, create=True, lock=True)
        security.totp_secret_enc = security.totp_pending_enc = None
        security.totp_enabled_at, security.totp_last_step = None, None
        self.db.execute(delete(AuthRecoveryCode).where(AuthRecoveryCode.user_id == user_id))
        self._end_all_sessions(user_id)
        self._audit("user.2fa_reset", user_id)
        self.db.commit()

    # ------------------------------------------------- коды восстановления

    def recovery_codes_left(self, user_id: str) -> int:
        return len(self.db.execute(select(AuthRecoveryCode.id).where(AuthRecoveryCode.user_id == user_id,
                                                                     AuthRecoveryCode.used_at.is_(None))).all())

    def regenerate_recovery_codes(self, user_id: str, password: str, code: str, secret_key: str) -> list[str]:
        """Новые коды восстановления (старые перестают действовать): нужны пароль и свежий код из приложения."""
        user = self.get_user(user_id)
        self._require_password(user, password)
        security = self._security(user_id, lock=True)
        if security is None or not security.totp_secret_enc:
            raise MfaStateError("Второй фактор не включён", "mfa_not_enabled")
        step = totp.verify(self._decrypt(security.totp_secret_enc, secret_key), code, not_before=security.totp_last_step)
        if step is None:
            raise MfaCodeError("Код не подошёл или уже был использован — дождитесь следующего кода в приложении")
        security.totp_last_step = step
        codes = self._replace_recovery_codes(user_id, secret_key)
        self.db.commit()
        return codes

    def _replace_recovery_codes(self, user_id: str, secret_key: str) -> list[str]:
        key = self._recovery_key(secret_key)
        self.db.execute(delete(AuthRecoveryCode).where(AuthRecoveryCode.user_id == user_id))
        codes = recovery_codes.generate()
        now = utcnow()
        for code in codes:
            self.db.add(AuthRecoveryCode(user_id=user_id, code_hash=recovery_codes.digest(code, key), created_at=now))
        return codes

    @staticmethod
    def _recovery_key(secret_key: str) -> bytes:
        try:
            return auth_secrets.recovery_hmac_key(secret_key)
        except auth_secrets.AuthSecretError as exc:
            raise MfaKeyError(str(exc)) from None

    def start_mfa_challenge(self, user: User, ttl_minutes: int) -> tuple[str, int]:
        """Пароль верный, второй фактор включён: одноразовый пропуск второго шага (в базе — только хеш)."""
        now = utcnow()
        self.db.execute(delete(AuthMfaChallenge).where(AuthMfaChallenge.user_id == user.id, AuthMfaChallenge.expires_at <= now))
        raw = generate_token()
        self.db.add(AuthMfaChallenge(user_id=user.id, pass_hash=hash_token(raw), created_at=now,
                                     expires_at=now + timedelta(minutes=ttl_minutes)))
        self.db.commit()
        return raw, ttl_minutes * 60

    def mfa_challenge_username(self, raw_pass: str) -> str | None:
        """Чей пропуск второго шага (для ограничения попыток по логину ДО проверки кода). Неизвестный пропуск — None."""
        challenge = self.db.execute(
            select(AuthMfaChallenge).where(AuthMfaChallenge.pass_hash == hash_token(raw_pass or ""))
        ).scalar_one_or_none()
        user = self.db.get(User, challenge.user_id) if challenge is not None else None
        return user.username if user is not None else None

    def complete_mfa_challenge(self, raw_pass: str, code: str, secret_key: str, *, max_attempts: int) -> MfaResult:
        """Второй шаг входа. Успех — пропуск израсходован, шаг кода запомнен (повтор того же кода не пройдёт), возвращается
        пользователь (маршрут выдаёт сеанс с отметкой «второй фактор пройден»). Неверный код — счётчик попыток; исчерпан —
        пропуск сгорает и нужен вход заново с паролем."""
        now = utcnow()
        challenge = self.db.execute(
            select(AuthMfaChallenge).where(AuthMfaChallenge.pass_hash == hash_token(raw_pass or "")).with_for_update()
        ).scalar_one_or_none()
        if challenge is None or challenge.used_at is not None or token_is_expired(challenge.expires_at, now):
            raise MfaChallengeError("Время на ввод кода истекло — войдите заново")
        user = self.db.get(User, challenge.user_id)
        security = self._security(challenge.user_id, lock=True)
        if user is None or not user.is_active or security is None or not security.totp_secret_enc:
            challenge.used_at = now
            self.db.commit()
            raise MfaChallengeError("Войдите заново")
        recovery_row = None
        step = None
        if recovery_codes.looks_like_recovery(code):
            # Код восстановления (потерян телефон): одноразовый, в базе только HMAC; строка блокируется — один код не пройдёт дважды
            # и при двух одновременных запросах.
            recovery_row = self.db.execute(
                select(AuthRecoveryCode).where(AuthRecoveryCode.user_id == user.id, AuthRecoveryCode.used_at.is_(None),
                                               AuthRecoveryCode.code_hash == recovery_codes.digest(code, self._recovery_key(secret_key)))
                .with_for_update()
            ).scalars().first()
        else:
            step = totp.verify(self._decrypt(security.totp_secret_enc, secret_key), code, not_before=security.totp_last_step)
        if step is None and recovery_row is None:
            challenge.failed_attempts = int(challenge.failed_attempts or 0) + 1
            left = max(0, max_attempts - challenge.failed_attempts)
            if left == 0:
                challenge.used_at = now
            self.db.commit()
            error = (MfaChallengeError("Слишком много неверных кодов — войдите заново", "mfa_attempts_exhausted") if left == 0
                     else MfaCodeError("Код не подошёл или уже был использован", params={"attempts_left": left}))
            error.username = user.username          # для общего ограничения попыток входа; клиенту не отдаётся
            raise error
        challenge.used_at = now
        if recovery_row is not None:
            recovery_row.used_at = now
            security.updated_at = now
            self.db.commit()
            return MfaResult(user=user, recovery_used=True, recovery_left=self.recovery_codes_left(user.id))
        security.totp_last_step, security.updated_at = step, now
        self.db.commit()
        return MfaResult(user=user)

    def _require_password(self, user: User, password: str) -> None:
        if not verify_password(password or "", user.password_hash):
            raise WrongPasswordError("Пароль неверный")

    @staticmethod
    def _encrypt(secret: str, secret_key: str) -> str:
        try:
            return auth_secrets.encrypt_totp_secret(secret, secret_key)
        except auth_secrets.AuthSecretError as exc:
            raise MfaKeyError(str(exc)) from None

    @staticmethod
    def _decrypt(token: str, secret_key: str) -> str:
        try:
            return auth_secrets.decrypt_totp_secret(token, secret_key)
        except auth_secrets.AuthSecretError as exc:
            raise MfaKeyError(str(exc)) from None

    def _security(self, user_id: str, *, create: bool = False, lock: bool = False) -> UserSecurity | None:
        query = select(UserSecurity).where(UserSecurity.user_id == user_id)
        if lock:
            query = query.with_for_update()
        security = self.db.execute(query).scalar_one_or_none()
        if security is None and create:
            security = UserSecurity(user_id=user_id, session_epoch=0, updated_at=utcnow())
            self.db.add(security)
            self.db.flush()
        return security

    # ---------------------------------------------------------- пользователи

    def create_user(
        self, username: str, password: str, role: Role, store_ids: list[str] | None = None,
        access_until: datetime | None = None,
    ) -> User:
        """Создаёт пользователя. Бросает PolicyViolation (логин/пароль/срок), UserAlreadyExistsError, UserArchivedError (логин
        архивного сотрудника), UnknownStoreError. access_until — «доступ до» (документ 05, §12.1), по желанию."""
        canon = validate_username(username)
        check_password_policy(password)

        taken = self.db.execute(select(User.archived_at).where(User.username == canon)).first()
        if taken is not None:
            if taken[0] is not None:          # документ 05, §12.1 п. 1: логин архивных сотрудников повторно не используется
                raise UserArchivedError(f"Логин «{canon}» был у сотрудника, который сейчас в архиве. Придумайте другой логин",
                                        "username_archived", params={"username": canon})
            raise UserAlreadyExistsError(f"Пользователь «{canon}» уже существует", params={"username": canon})
        if access_until is not None and account_status.access_expired(access_until, utcnow()):
            raise PolicyViolation("Дата «доступ до» уже прошла — укажите будущую дату или оставьте поле пустым", "access_until_past")

        wanted_stores = set(store_ids or [])
        self._ensure_stores_exist(wanted_stores)

        user = User(
            username=canon,
            password_hash=hash_password(password),
            role=role.value,
            is_active=True,
            access_until=access_until,
            store_access=[UserStoreAccess(store_id=s) for s in sorted(wanted_stores)],
        )
        self.db.add(user)
        try:
            self.db.flush()
            self._audit("user.created", user.id, after={"username": canon, "role": role.value, "store_ids": sorted(wanted_stores),
                                                        "access_until": access_until})
            self.db.commit()
        except IntegrityError:
            # Гонка: между проверкой _username_taken и INSERT другой запрос успел создать тот же логин
            # (unique-индекс сработал). Раньше это было 500 — теперь тот же 409, что и без гонки.
            self.db.rollback()
            raise UserAlreadyExistsError(f"Пользователь «{canon}» уже существует", params={"username": canon})
        self.db.refresh(user)
        return user

    def set_language(self, user_id: str, language: str | None) -> str | None:
        """Язык интерфейса в профиле (миграция 0002). language — уже проверенный код из LANGUAGES или None («не выбран»)."""
        user = self.get_user(user_id)
        user.language = language
        self.db.commit()
        return user.language

    def get_user(self, user_id: str) -> User:
        user = self.db.get(User, user_id)
        if user is None:
            raise UserNotFoundError(f"Пользователь {user_id} не найден")
        return user

    def list_users(self) -> list[User]:
        return list(self.db.execute(select(User).order_by(User.username)).scalars().all())

    def update_user(
        self,
        user_id: str,
        *,
        role: Role | None = None,
        is_active: bool | None = None,
        store_ids: list[str] | None = None,
        password: str | None = None,
        block_reason: str | None = None,
        access_until: datetime | None = None,
        clear_access_until: bool = False,
    ) -> User:
        """Частичное обновление. Смена пароля и блокировка сразу отзывают все токены пользователя.
        is_active=False — блокировка (с необязательной причиной, документ 05, §12.5), True — разблокировка (сеансы не возвращаются).
        access_until — «доступ до» (§12.1), clear_access_until — снять срок. Архивную учётную запись менять нельзя (§12.7)."""
        user = self.get_user(user_id)
        if user.archived_at is not None:
            raise UserArchivedError("Сотрудник в архиве: доступ отозван, учётную запись изменить нельзя")
        now = utcnow()
        if access_until is not None and account_status.access_expired(access_until, now):
            raise PolicyViolation("Дата «доступ до» уже прошла — укажите будущую дату или снимите срок", "access_until_past")
        was = self._profile(user)

        # Сначала вся валидация, потом изменения: при ошибке в середине не остаётся
        # наполовину применённого обновления в сессии.
        if password is not None:
            check_password_policy(password)
        wanted_stores = set(store_ids) if store_ids is not None else None
        if wanted_stores is not None:
            self._ensure_stores_exist(wanted_stores)

        revoke_tokens = False
        if role is not None:
            revoke_tokens = role.value != user.role            # права меняются — сеансы открываются заново (с проверкой 2FA)
            user.role = role.value
        if is_active is not None:
            if not is_active and user.is_active:
                user.blocked_at = now
            if not is_active:
                user.block_reason = account_status.clean_reason(block_reason) or user.block_reason
            else:
                user.blocked_at, user.block_reason = None, None
            user.is_active = is_active
            revoke_tokens = revoke_tokens or not is_active
        if access_until is not None:
            user.access_until = access_until
        elif clear_access_until:
            user.access_until = None
        if password is not None:
            user.password_hash = hash_password(password)
            revoke_tokens = True
        if wanted_stores is not None:
            self._replace_store_access(user, wanted_stores)

        if revoke_tokens:
            self._end_all_sessions(user.id)       # в той же транзакции, что и новый пароль / отключение (документ 05, §12.11)
        self.db.flush()
        became = self._profile(user)
        if was["is_active"] != became["is_active"]:
            self._audit("user.blocked" if not became["is_active"] else "user.unblocked", user.id,
                        reason=user.block_reason if not became["is_active"] else None)
        old, new = audit.changes({k: v for k, v in was.items() if k != "is_active"}, {k: v for k, v in became.items() if k != "is_active"})
        if new:
            self._audit("user.updated", user.id, before=old, after=new)
        if password is not None:
            self._audit("user.password_reset", user.id)
        self.db.commit()
        self.db.refresh(user)
        return user

    def archive_user(self, user_id: str, reason: str | None = None) -> User:
        """«Отозвать доступ» (документ 05, §12.7): блокировка + архив одним действием. Запись не удаляется — на неё ссылается журнал;
        все сеансы завершаются в той же транзакции. Повторный вызов для архивной записи — без изменений."""
        user = self.get_user(user_id)
        if user.archived_at is not None:
            return user
        before = self._profile(user)
        now = utcnow()
        if user.is_active:
            user.blocked_at = now
        user.is_active = False
        user.archived_at = now
        user.block_reason = account_status.clean_reason(reason) or user.block_reason
        self._end_all_sessions(user.id)
        self._audit("user.archived", user.id, before=before, after={"status": "archived"}, reason=user.block_reason)
        self.db.commit()
        self.db.refresh(user)
        return user

    def account_state(self, user: User) -> str:
        must_change, _deadline = self.temporary_password_state(user.id)
        return account_status.status_of(is_active=user.is_active, archived_at=user.archived_at, access_until=user.access_until,
                                        must_change_password=must_change, now=utcnow())

    def access_expired(self, user: User) -> bool:
        return account_status.access_expired(user.access_until, utcnow())

    # ------------------------------------------------------------- внутреннее

    def _username_taken(self, canon: str) -> bool:
        return self.db.execute(select(User.id).where(User.username == canon)).first() is not None

    def _ensure_stores_exist(self, store_ids: set[str]) -> None:
        if not store_ids:
            return
        found = set(self.db.execute(select(Store.id).where(Store.id.in_(store_ids))).scalars().all())
        missing = store_ids - found
        if missing:
            raise UnknownStoreError(f"Магазины не найдены: {', '.join(sorted(missing))}", params={"ids": ", ".join(sorted(missing))})

    def _replace_store_access(self, user: User, wanted: set[str]) -> None:
        # Считаем разницу и трогаем только изменившиеся строки: пересоздание строк с тем же
        # составным ключом (user_id, store_id) в одном flush — лишний риск конфликта ключей.
        existing = {a.store_id: a for a in user.store_access}
        for store_id, access in existing.items():
            if store_id not in wanted:
                user.store_access.remove(access)
        for store_id in sorted(wanted - existing.keys()):
            user.store_access.append(UserStoreAccess(store_id=store_id))
