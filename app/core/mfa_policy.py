"""
Правила входа администраторов (решения владельца 2026-11-08, документ 18, §7): чистая логика без FastAPI/SQLAlchemy —
tests/test_mfa_policy.py. Сервис и маршруты только применяют эти решения.

  В-1 VPN: административный API доступен только на «административном входе» программы (отдельный порт, опубликованный лишь на
      127.0.0.1 и на адресе VPN — docker-compose.yml, служба admin-api). На обычном входе (порт 8000, локальная сеть магазина)
      разрешения управления не действуют, какой бы токен ни пришёл.
  В-2, В-3: второй фактор обязателен ВСЕМ администраторам, в том числе на компьютере магазина. Администратор без настроенного
      второго фактора получает после пароля только «пропуск настройки» — токен, годный лишь для настройки второго фактора, и только
      на административном входе. На обычном входе — отказ с подсказкой.
  В-4: сеанс администратора и любой сеанс административного входа живёт не дольше 8 часов от проверки пароля и кода; продление
      (обмен токена) не сдвигает этот предел.
  Важные операции требуют недавнего подтверждения личности (пароль и код не старше 10 минут).
"""
from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

ISSUE = "issue"                     # выдать сеанс
ASK_CODE = "ask_code"               # спросить код из приложения (второй шаг входа)
ENROLL = "enroll"                   # выдать пропуск настройки второго фактора (только административный вход)
ENROLL_ELSEWHERE = "enroll_elsewhere"   # отказать: настраивать второй фактор можно только на административном входе

SCOPE_FULL = "full"                 # обычный сеанс
SCOPE_PASSWORD = "password_change"  # пропуск «смена временного пароля»: годен только для POST /auth/password/initial (документ 05, §12.2)
SCOPE_ENROLL = "enroll"             # пропуск настройки второго фактора: годен только для /auth/2fa/setup и /auth/2fa/confirm

LISTENER_PUBLIC = "public"          # порт 8000: страницы и API для сотрудников (локальная сеть магазина)
LISTENER_ADMIN = "admin"            # порт 8001: управление; публикуется только на 127.0.0.1 и адресе VPN
LISTENER_ALL = "all"                # один процесс без разделения (запуск без Docker): управление — только с самого компьютера
                                    # (запрос пришёл на 127.0.0.1/::1, без заголовков прокси) — effective_listener()
LISTENERS = (LISTENER_PUBLIC, LISTENER_ADMIN, LISTENER_ALL)

ENROLL_TTL_MINUTES = 10


def normalize_listener(value: str | None) -> str:
    value = (value or LISTENER_ALL).strip().lower()
    return value if value in LISTENERS else LISTENER_PUBLIC          # опечатка в настройке -> самый строгий вариант


def admin_api_allowed(listener: str) -> bool:
    """Действуют ли разрешения управления на этом входе программы."""
    return normalize_listener(listener) in (LISTENER_ADMIN, LISTENER_ALL)


# ------------------------------------------------- какой вход у ЭТОГО запроса (аудит 2026-11-08, документ 18, §12)

LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
# Заголовки, которые ставит обратный прокси. Браузер на самом компьютере их не шлёт; есть они — запрос пришёл через прокси, и то,
# что он пришёл на 127.0.0.1, ничего не говорит о настоящем клиенте.
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip")


def is_loopback(host: str | None) -> bool:
    """127.0.0.0/8, ::1 и ::ffff:127.x — да; имя, пустое значение, адрес сети — нет."""
    try:
        addr = ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool(addr.is_loopback or (mapped is not None and mapped.is_loopback))


def host_from_header(value: str | None) -> str:
    """Заголовок Host -> имя без порта, в нижнем регистре: «LocalHost:8001» -> «localhost», «[::1]:8001» -> «::1». Нет — ""."""
    raw = (value or "").strip().lower()
    if raw.startswith("["):
        return raw[1:raw.find("]")] if "]" in raw else ""
    if raw.count(":") == 1:
        raw = raw.split(":", 1)[0]
    return raw.rstrip(".")


def allowed_admin_hosts(vpn_bind_ip: str | None = None, extra: str | None = None) -> frozenset[str]:
    """Имена, по которым открывают административный вход: этот компьютер, адрес VPN (VPN_BIND_IP) и явный список
    ADMIN_ALLOWED_HOSTS (через запятую, например имя компьютера в Tailscale). Защита от подмены DNS (DNS rebinding):
    чужая страница в браузере на этом компьютере обращается к localhost:8001 под своим именем — управления она не получит."""
    names = set(LOCAL_HOSTNAMES)
    for item in [vpn_bind_ip or "", *(extra or "").split(",")]:
        name = host_from_header(item)
        if name:
            names.add(name)
    return frozenset(names)


def has_forwarding_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> bool:
    keys = {k.lower() for k in (headers.keys() if isinstance(headers, Mapping) else (k for k, _ in headers))}
    return any(h in keys for h in FORWARDING_HEADERS)


def effective_listener(configured: str | None, *, server_host: str | None, host_header: str | None, forwarded: bool,
                       allowed_hosts: Iterable[str]) -> str:
    """Каким входом считать ЭТОТ запрос. Решает, действуют ли права управления (admin) или нет (public).

      public — всегда public;
      admin  — admin, если запрос адресован разрешённому имени (allowed_admin_hosts), иначе public (подмена DNS);
      all    — admin, только если запрос пришёл на петлевой адрес сервера (scope["server"] — адрес сокета, который клиент
               подделать не может), без заголовков прокси и по разрешённому имени; иначе public. Так запуск без Docker на 0.0.0.0
               (scripts/start_demo.py: телефоны в Wi-Fi) не открывает управление сети магазина.
    """
    listener = normalize_listener(configured)
    if listener == LISTENER_PUBLIC:
        return LISTENER_PUBLIC
    host_ok = host_from_header(host_header) in set(allowed_hosts)
    if listener == LISTENER_ADMIN:
        return LISTENER_ADMIN if host_ok else LISTENER_PUBLIC
    return LISTENER_ADMIN if (is_loopback(server_host) and not forwarded and host_ok) else LISTENER_PUBLIC


def listener_for_request(configured: str | None, scope: Mapping, headers: Mapping[str, str], *, vpn_bind_ip: str | None = None,
                         extra_hosts: str | None = None) -> str:
    """То же для запроса ASGI: адрес сокета сервера — scope["server"] (uvicorn берёт его у сокета, заголовки на него не влияют,
    в том числе --proxy-headers), имя — заголовок Host. Проверено на настоящем uvicorn: tests/test_real_http_server.py."""
    server = scope.get("server") or (None, None)
    return effective_listener(configured, server_host=server[0], host_header=headers.get("host"),
                              forwarded=has_forwarding_headers(headers),
                              allowed_hosts=allowed_admin_hosts(vpn_bind_ip, extra_hosts))


def admin_rights_apply(request_listener: str, session_listener: str | None) -> bool:
    """Действуют ли права управления: запрос пришёл на административный вход И сеанс открыт там же (пароль и код вводились
    на административном входе). Сеанс, открытый на входе сотрудников (пароль и код шли по Wi-Fi магазина без HTTPS), прав
    управления не даёт нигде; сеанс без сведений (до 2026-11-08) — тоже."""
    return request_listener == LISTENER_ADMIN and session_listener == LISTENER_ADMIN


def after_password(*, totp_enabled: bool, is_admin: bool, require_for_admins: bool, listener: str) -> str:
    """Что делать после верного пароля.
    Второй фактор включён — код спрашивается всегда, где бы ни был вход (иначе пароль за компьютером магазина обходил бы его)."""
    if totp_enabled:
        return ASK_CODE
    if is_admin and require_for_admins:
        return ENROLL if admin_api_allowed(listener) else ENROLL_ELSEWHERE
    return ISSUE


def session_accepted(*, is_admin: bool, require_for_admins: bool, scope: str, mfa: bool) -> bool:
    """Годен ли сеанс для обычных запросов. Пропуск настройки — нет; сеанс администратора без пройденного второго фактора — нет
    (так закрываются и старые токены, выданные до решения В-3, и любой путь выдачи токена в обход второго шага)."""
    if scope != SCOPE_FULL:
        return False
    return not (is_admin and require_for_admins and not mfa)


def session_lifetime_minutes(*, base_ttl_minutes: int, is_admin: bool, listener: str, admin_max_hours: int) -> int:
    """Срок нового сеанса: администратор и административный вход — не больше admin_max_hours (В-4)."""
    limit = base_ttl_minutes
    if is_admin or normalize_listener(listener) == LISTENER_ADMIN:
        limit = min(limit, admin_max_hours * 60)
    return max(1, limit)


def renewed_expiry(*, now: datetime, auth_at: datetime, ttl_minutes: int, max_hours: int | None) -> datetime | None:
    """Новый срок при продлении (обмене) токена: now + ttl, но не позже auth_at + max_hours. None — продлевать нельзя (предел)."""
    expiry = now + timedelta(minutes=ttl_minutes)
    if max_hours is not None:
        expiry = min(expiry, auth_at + timedelta(hours=max_hours))
    return expiry if expiry > now + timedelta(minutes=1) else None


def recently_authenticated(reauth_at: datetime | None, now: datetime, window_minutes: int) -> bool:
    return reauth_at is not None and timedelta(0) <= now - reauth_at <= timedelta(minutes=window_minutes)


# ----------------------------------------------------------------- владелец (документ 05, §8)

def owner_guard(*, actor_is_owner: bool, target_is_owner: bool, actor_id: str, target_id: str) -> str | None:
    """Может ли администратор управлять учётной записью (роль, пароль, отключение, сеансы, второй фактор).
    Возвращает код отказа или None. Владельца меняет только сам владелец (свою роль и отключение себя запрещает другое правило)."""
    if target_is_owner and not actor_is_owner and actor_id != target_id:
        return "owner_protected"
    return None


def startup_warnings(*, require_for_admins: bool, key_configured: bool, listener: str | None) -> list[str]:
    """Предупреждения в журнал при запуске: опасная или нерабочая настройка входа видна сразу, а не после жалобы «не могу войти»."""
    out = []
    if not require_for_admins:
        out.append("ВНИМАНИЕ: auth.require_2fa_for_admins выключен — администраторы входят БЕЗ второго фактора (решения В-2, В-3 "
                   "не соблюдаются). Верните true в configs/config.yaml и перезапустите.")
    elif not key_configured:
        out.append("ВНИМАНИЕ: не задан AUTH_SECRET_KEY — администраторы не смогут настроить второй фактор и войти. Создайте ключ: "
                   "python -m scripts.generate_secret_key --auth (docs/REMOTE_ACCESS_GUIDE.md, п. 2).")
    if normalize_listener(listener) == LISTENER_ALL:
        out.append("Вход программы: all — управление доступно только с самого компьютера (http://localhost), из сети — нет.")
    return out
