"""
Ролевая модель доступа (F13) и криптографические примитивы аутентификации.

Модуль ОСОЗНАННО не импортирует FastAPI/SQLAlchemy/pydantic — только stdlib.
Благодаря этому вся логика решений («кто что может», «верен ли пароль»,
«не просрочен ли токен») выполняется и тестируется в любой среде, в том числе
в офлайн-песочнице (tools/offline_test_harness.py). Слои, которые ходят в БД
(app/services/auth_service.py) и в HTTP (app/api/deps.py), — тонкие обёртки
поверх этих функций.

Модель доступа — два независимых вопроса:
  1. ПРАВО (Permission): может ли роль вообще выполнять такое действие?
  2. СКОУП (магазины): к каким магазинам это относится? Мерчандайзер и
     региональный менеджер видят только назначенные им магазины; аналитик и
     админ — все.

Токены — непрозрачные (opaque), а не JWT: в БД хранится только SHA-256 хеш
токена, поэтому утечка дампа БД не даёт рабочих токенов, а отзыв (logout,
деактивация пользователя) мгновенный — достаточно удалить строку.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache

from app.core.app_errors import AppError


class PolicyViolation(AppError, ValueError):
    """Значение не проходит политику (логин/пароль/идентификатор пути). Вид invalid (HTTP 422)."""

    kind = "invalid"
    default_code = "policy_violation"


# ---------------------------------------------------------------------------
# Роли и права
# ---------------------------------------------------------------------------


class Role(str, Enum):
    MERCHANDISER = "merchandiser"          # мерчандайзер: снимает полки, видит результат по своим магазинам
    REGIONAL_MANAGER = "regional_manager"  # региональный менеджер: смотрит отчёты по своим магазинам
    ANALYST = "analyst"                    # аналитик: только чтение, по всей сети
    ADMIN = "admin"                        # админ: всё, включая каталог и пользователей
    CAMERA_AGENT = "camera_agent"          # служебная учётка агента камер (scripts/camera_agent.py --managed): только загрузка снимков и список камер


class Permission(str, Enum):
    SNAPSHOT_UPLOAD = "snapshot:upload"  # POST /api/v1/snapshots
    REPORT_READ = "report:read"          # статус/отчёт снимка, OSA-сводка, дашборд, экспорт (F14)
    REVIEW_QUEUE = "review:manage"       # F12: очередь неуверенных распознаваний, ручное решение
    CATALOG_READ = "catalog:read"        # просмотр/поиск каталога (нужно при "corrected" в F12)
    CATALOG_WRITE = "catalog:write"      # добавление SKU в каталог
    USER_MANAGE = "user:manage"          # создание/изменение пользователей
    STORE_MANAGE = "store:manage"        # создание магазинов и полок (страница frontend/setup.html); как и USER_MANAGE — только admin
    CAMERA_MANAGE = "camera:manage"      # камеры: просмотр, создание, правка, команды «снимок сейчас»/«проверка» (админ-панель); только admin
    ASSISTANT_USE = "assistant:use"      # ИИ-помощник «только смотреть» (документ 16, этап A1): пока только владелец (admin)
    CAMERA_AGENT = "camera:agent"        # агент камер: получить список камер (с адресами источников) и сообщать результаты; только роль camera_agent и admin


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    # F12: мерчандайзер разбирает очередь неуверенных распознаваний со своих же полок —
    # он физически стоит у стеллажа и может визуально решить, тот ли это товар, лучше,
    # чем кто-либо ещё. Региональный менеджер и аналитик — надзорные роли (только чтение),
    # им очередь ревью намеренно не даётся: решение о правках эмбеддинг-индекса каталога
    # (см. review_service.py) — операционное, а не управленческое.
    #
    # CATALOG_READ — у ВСЕХ ролей: просмотр/поиск SKU по названию/бренду не меняет ничего
    # (в отличие от CATALOG_WRITE — только admin) и низкорискован сам по себе, поэтому не
    # ограничивается так же строго, как редактирование; нужен и мерчандайзеру при "corrected"
    # (F12, найти правильный товар), и остальным ролям просто для понимания покрытия каталога.
    Role.MERCHANDISER: frozenset({
        Permission.SNAPSHOT_UPLOAD, Permission.REPORT_READ, Permission.REVIEW_QUEUE, Permission.CATALOG_READ,
    }),
    Role.REGIONAL_MANAGER: frozenset({Permission.REPORT_READ, Permission.CATALOG_READ}),
    Role.ANALYST: frozenset({Permission.REPORT_READ, Permission.CATALOG_READ}),
    Role.ADMIN: frozenset(Permission),
    # Агент видит только свои магазины (ALL_STORES_ROLES его не включает): украденная учётка агента не даёт ни чтения отчётов,
    # ни управления пользователями — только загрузку снимков и чтение списка камер своих магазинов.
    Role.CAMERA_AGENT: frozenset({Permission.SNAPSHOT_UPLOAD, Permission.CAMERA_AGENT}),
}

# Права, которые есть ТОЛЬКО у администратора: управление пользователями, магазинами и полками, камерами и правка каталога.
# Выводятся из таблицы ролей, а не перечисляются вручную: новое право «только для admin» автоматически попадает сюда. Действуют
# только на административном входе (с компьютера магазина или через VPN) и только в сеансе, открытом там же — deps.require_permission,
# решение владельца по В-5 (2026-11-08, документ 18, §13). CAMERA_AGENT сюда не входит: оно есть и у роли camera_agent.
ADMIN_ONLY_PERMISSIONS: frozenset[Permission] = frozenset(
    p for p in Permission if {role for role, perms in ROLE_PERMISSIONS.items() if p in perms} == {Role.ADMIN}
)

# Роли, у которых скоуп — вся сеть (список назначенных магазинов не учитывается).
ALL_STORES_ROLES: frozenset[Role] = frozenset({Role.ANALYST, Role.ADMIN})


def parse_role(value: str | Role | None) -> Role | None:
    """Строка из БД -> Role. Неизвестное значение -> None (= нет никаких прав, fail-closed)."""
    if isinstance(value, Role):
        return value
    try:
        return Role(value)
    except ValueError:
        return None


def role_has_permission(role: Role | None, permission: Permission) -> bool:
    if role is None:
        return False
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def can_access_store(role: Role | None, assigned_store_ids: Collection[str], store_id: str) -> bool:
    if role is None:
        return False
    return role in ALL_STORES_ROLES or store_id in assigned_store_ids


@dataclass(frozen=True)
class CurrentUser:
    """Аутентифицированный пользователь в момент запроса — без привязки к ORM-сессии."""

    id: str
    username: str
    role: Role
    store_ids: frozenset[str] = field(default_factory=frozenset)
    session_id: str | None = None          # id записи api_tokens этого запроса («текущий сеанс» в списке сеансов)
    session_scope: str = "full"            # full | enroll (пропуск настройки второго фактора, документ 18)
    mfa: bool = False                      # сеанс открыт со вторым фактором
    reauth_at: datetime | None = None      # последнее подтверждение личности (пароль и код) — для важных операций
    is_owner: bool = False                 # владелец системы (документ 05, §8)
    session_listener: str | None = None    # на каком входе открыт сеанс (admin / public; None — сеанс до 2026-11-08)

    def has_permission(self, permission: Permission) -> bool:
        return role_has_permission(self.role, permission)

    def can_access_store(self, store_id: str) -> bool:
        return can_access_store(self.role, self.store_ids, store_id)


# ---------------------------------------------------------------------------
# Логин и пароль
# ---------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._@-]{2,63}$")


def canonical_username(username: str) -> str:
    """Каноничная форма логина для поиска: без пробелов по краям и в нижнем регистре."""
    return (username or "").strip().lower()


def validate_username(username: str) -> str:
    """Проверяет логин для СОЗДАНИЯ пользователя и возвращает каноничную форму."""
    canon = canonical_username(username)
    if not _USERNAME_RE.match(canon):
        raise PolicyViolation(
            "Логин: 3–64 символа, латинские буквы/цифры и . _ @ - (начинается с буквы или цифры)", "username_invalid"
        )
    return canon


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PolicyViolation(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов", "password_too_short", {"min": MIN_PASSWORD_LENGTH})
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PolicyViolation(f"Пароль должен быть не длиннее {MAX_PASSWORD_LENGTH} символов", "password_too_long", {"max": MAX_PASSWORD_LENGTH})


# scrypt из stdlib: без сторонних зависимостей (passlib/bcrypt/argon2), параметры
# зашиты в сам хеш — их можно повышать позже, старые хеши продолжат проверяться.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16
_SCRYPT_MAXMEM = 64 * 1024 * 1024  # с запасом под будущее повышение N


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=_SCRYPT_MAXMEM
    )


def hash_password(password: str) -> str:
    """Формат: scrypt$N$r$p$salt_b64$hash_b64. Политику пароля проверяйте отдельно (check_password_policy)."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time сверка. Любой мусор в stored_hash -> False, а не исключение."""
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored_hash.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        actual = _scrypt(password, salt, int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """Хеш-пустышка: при логине несуществующего пользователя тоже тратим время на scrypt,
    чтобы по времени ответа нельзя было отличить «нет такого логина» от «неверный пароль»."""
    return hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------------------
# Токены
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Наивное UTC-время — как datetime.utcnow() в остальном коде/моделях, но без DeprecationWarning."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def generate_token() -> str:
    return secrets.token_urlsafe(32)  # 256 бит энтропии


def hash_token(token: str) -> str:
    # Токен — случайные 256 бит, а не пароль: медленный хеш не нужен, достаточно SHA-256.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_is_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    return (now or utcnow()) >= expires_at


# ---------------------------------------------------------------------------
# Сегменты путей (store_id/shelf_id попадают в путь файла в routes_upload.py)
# ---------------------------------------------------------------------------

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def is_safe_path_segment(value: str) -> bool:
    """True, если строку безопасно использовать как ОДИН компонент пути.

    Разрешены латиница/цифры/._- ; нельзя начинать с точки, поэтому «.», «..»,
    скрытые файлы, разделители пути и абсолютные пути отсекаются автоматически.
    """
    return bool(_SAFE_SEGMENT_RE.match(value)) and ".." not in value


def safe_path_segment(value: str) -> str:
    """Безопасный компонент пути для произвольной строки от клиента — без отказа в запросе.

    Нормальные идентификаторы («shelf-demo-1», UUID) остаются как есть. Всё остальное
    (../, слэши, кириллица, пробелы, пустая строка) заменяется детерминированным
    хешем: путь в ФС нигде не несёт смысла (настоящий shelf_id хранится в БД),
    так что полка с «нестандартным» именем продолжает работать, а не получает 400.
    """
    if is_safe_path_segment(value):
        return value
    return "x-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
