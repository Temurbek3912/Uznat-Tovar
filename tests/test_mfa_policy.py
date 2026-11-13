"""app/core/mfa_policy.py — решения владельца В-1…В-4 как таблицы."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core import mfa_policy as mp

NOW = datetime(2026, 11, 8, 12, 0, 0)


@pytest.mark.parametrize("totp, admin, rule, listener, expected", [
    (True, True, True, "public", mp.ASK_CODE),         # включён — код спрашивается везде
    (True, False, True, "admin", mp.ASK_CODE),
    (True, True, False, "all", mp.ASK_CODE),
    (False, True, True, "admin", mp.ENROLL),           # В-3: админ без 2FA — только пропуск настройки
    (False, True, True, "all", mp.ENROLL),
    (False, True, True, "public", mp.ENROLL_ELSEWHERE),  # В-1: настраивать — только на административном входе
    (False, True, False, "public", mp.ISSUE),          # правило выключено — как раньше
    (False, False, True, "public", mp.ISSUE),          # сотрудники без 2FA входят как раньше
])
def test_after_password(totp, admin, rule, listener, expected):
    assert mp.after_password(totp_enabled=totp, is_admin=admin, require_for_admins=rule, listener=listener) == expected


@pytest.mark.parametrize("admin, rule, scope, mfa, ok", [
    (True, True, "full", True, True),
    (True, True, "full", False, False),                # старый токен админа или выданный в обход второго шага
    (True, True, "enroll", True, False),               # пропуск настройки — не сеанс
    (False, True, "enroll", False, False),
    (False, True, "full", False, True),
    (True, False, "full", False, True),
    (False, False, "other", True, False),              # неизвестная область — отказ
])
def test_session_accepted(admin, rule, scope, mfa, ok):
    assert mp.session_accepted(is_admin=admin, require_for_admins=rule, scope=scope, mfa=mfa) is ok


@pytest.mark.parametrize("listener, allowed", [("admin", True), ("all", True), ("public", False), ("PUBLIC ", False),
                                               ("typo", False), (None, True), ("", True)])
def test_admin_api_only_on_the_admin_listener(listener, allowed):
    assert mp.admin_api_allowed(listener) is allowed


def test_session_lifetime_is_capped_at_8_hours_for_admins_and_the_admin_listener():
    assert mp.session_lifetime_minutes(base_ttl_minutes=720, is_admin=True, listener="public", admin_max_hours=8) == 480
    assert mp.session_lifetime_minutes(base_ttl_minutes=720, is_admin=False, listener="admin", admin_max_hours=8) == 480
    assert mp.session_lifetime_minutes(base_ttl_minutes=720, is_admin=False, listener="public", admin_max_hours=8) == 720
    assert mp.session_lifetime_minutes(base_ttl_minutes=60, is_admin=True, listener="admin", admin_max_hours=8) == 60


def test_renewal_never_passes_the_absolute_limit():
    auth_at = NOW - timedelta(hours=7)
    assert mp.renewed_expiry(now=NOW, auth_at=auth_at, ttl_minutes=480, max_hours=8) == auth_at + timedelta(hours=8)
    assert mp.renewed_expiry(now=NOW, auth_at=NOW - timedelta(hours=8), ttl_minutes=480, max_hours=8) is None
    assert mp.renewed_expiry(now=NOW, auth_at=NOW - timedelta(hours=1), ttl_minutes=60, max_hours=8) == NOW + timedelta(minutes=60)
    assert mp.renewed_expiry(now=NOW, auth_at=NOW - timedelta(days=3), ttl_minutes=60, max_hours=None) == NOW + timedelta(minutes=60)


def test_recent_authentication_window():
    assert mp.recently_authenticated(NOW - timedelta(minutes=9), NOW, 10)
    assert not mp.recently_authenticated(NOW - timedelta(minutes=11), NOW, 10)
    assert not mp.recently_authenticated(None, NOW, 10)
    assert not mp.recently_authenticated(NOW + timedelta(minutes=5), NOW, 10)      # время из будущего не считается


@pytest.mark.parametrize("actor_owner, target_owner, same, refusal", [
    (False, True, False, "owner_protected"),            # администратор не управляет владельцем
    (True, True, True, None), (True, False, False, None), (False, False, False, None),
    (False, True, True, None),                          # (невозможный случай «не владелец сам себе владелец» не мешает)
])
def test_owner_guard(actor_owner, target_owner, same, refusal):
    assert mp.owner_guard(actor_is_owner=actor_owner, target_is_owner=target_owner, actor_id="a",
                          target_id="a" if same else "b") == refusal


# ----------------------------------------------------- вход ЭТОГО запроса (аудит 2026-11-08, документ 18, §12)

from app.core.mfa_policy import (  # noqa: E402
    LISTENER_ADMIN,
    LISTENER_PUBLIC,
    admin_rights_apply,
    allowed_admin_hosts,
    effective_listener,
    has_forwarding_headers,
    host_from_header,
    is_loopback,
    startup_warnings,
)

HOSTS = allowed_admin_hosts("100.64.0.5", "shop-pc.tail1234.ts.net, Shop-PC ")


def _eff(configured, server="127.0.0.1", host="localhost:8001", forwarded=False):
    return effective_listener(configured, server_host=server, host_header=host, forwarded=forwarded, allowed_hosts=HOSTS)


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("127.8.9.10", True), ("::1", True), ("[::1]", True), ("::ffff:127.0.0.1", True),
    ("192.168.1.20", False), ("100.64.0.5", False), ("172.18.0.1", False), ("localhost", False), ("", False), (None, False),
])
def test_loopback_is_decided_by_address_not_by_name(host, expected):
    assert is_loopback(host) is expected


@pytest.mark.parametrize("raw,expected", [
    ("localhost:8001", "localhost"), ("LOCALHOST", "localhost"), ("[::1]:8001", "::1"), ("::1", "::1"), ("100.64.0.5:8001", "100.64.0.5"),
    ("shop-pc.tail1234.ts.net.", "shop-pc.tail1234.ts.net"), ("", ""), (None, ""), ("[broken", ""),
])
def test_host_header_is_reduced_to_a_name(raw, expected):
    assert host_from_header(raw) == expected


def test_allowed_names_are_this_computer_the_vpn_address_and_the_explicit_list():
    assert HOSTS == {"localhost", "127.0.0.1", "::1", "100.64.0.5", "shop-pc.tail1234.ts.net", "shop-pc"}
    assert allowed_admin_hosts() == {"localhost", "127.0.0.1", "::1"}


def test_employee_listener_never_becomes_admin():
    assert _eff("public") == LISTENER_PUBLIC
    assert _eff("public", server="127.0.0.1", host="localhost") == LISTENER_PUBLIC


def test_admin_listener_is_admin_only_under_an_allowed_name():
    assert _eff("admin", server="172.18.0.5", host="localhost:8001") == LISTENER_ADMIN        # Docker: адрес контейнера — норма
    assert _eff("admin", server="172.18.0.5", host="100.64.0.5:8001") == LISTENER_ADMIN       # через VPN
    assert _eff("admin", server="172.18.0.5", host="shop-pc.tail1234.ts.net") == LISTENER_ADMIN
    assert _eff("admin", server="172.18.0.5", host="evil.example:8001") == LISTENER_PUBLIC    # подмена DNS: чужое имя
    assert _eff("admin", server="172.18.0.5", host=None) == LISTENER_PUBLIC
    assert _eff("admin", server="172.18.0.5", host="localhost", forwarded=True) == LISTENER_ADMIN   # «tailscale serve» к 127.0.0.1


def test_single_process_mode_gives_admin_only_to_requests_on_this_computer():
    """Найдено аудитом: scripts/start_demo.py запускает uvicorn на 0.0.0.0 (телефоны в Wi-Fi) без APP_LISTENER, а настройка «all»
    раньше давала права управления каждому запросу — то есть сети магазина."""
    assert _eff("all", server="127.0.0.1", host="localhost:8000") == LISTENER_ADMIN
    assert _eff("all", server="::1", host="[::1]:8000") == LISTENER_ADMIN
    assert _eff("all", server="192.168.1.20", host="192.168.1.20:8000") == LISTENER_PUBLIC     # телефон в Wi-Fi
    assert _eff("all", server="192.168.1.20", host="localhost") == LISTENER_PUBLIC            # Host подделан — адрес сокета нет
    assert _eff("all", server="127.0.0.1", host="localhost", forwarded=True) == LISTENER_PUBLIC   # обратный прокси на этом компьютере
    assert _eff("all", server="127.0.0.1", host="evil.example") == LISTENER_PUBLIC              # подмена DNS
    assert _eff("all", server=None, host="localhost") == LISTENER_PUBLIC                        # адрес неизвестен — строже
    assert _eff(None, server="192.168.1.20", host="localhost") == LISTENER_PUBLIC               # настройка не задана
    assert _eff("опечатка", server="127.0.0.1", host="localhost") == LISTENER_PUBLIC


def test_forwarding_headers_are_recognised_in_any_case():
    assert has_forwarding_headers({"X-Forwarded-For": "1.2.3.4"}) and has_forwarding_headers([("forwarded", "for=1.2.3.4")])
    assert has_forwarding_headers({"X-Real-IP": "1.2.3.4"})
    assert not has_forwarding_headers({"host": "localhost", "user-agent": "x"})


def test_admin_rights_need_the_admin_listener_for_both_the_request_and_the_session():
    assert admin_rights_apply(LISTENER_ADMIN, LISTENER_ADMIN)
    assert not admin_rights_apply(LISTENER_ADMIN, LISTENER_PUBLIC)    # пароль и код вводились по Wi-Fi без HTTPS
    assert not admin_rights_apply(LISTENER_ADMIN, None)               # сеанс без сведений (до 2026-11-08)
    assert not admin_rights_apply(LISTENER_ADMIN, "all")              # старая запись «all» — не административная
    assert not admin_rights_apply(LISTENER_PUBLIC, LISTENER_ADMIN)


def test_startup_warnings_name_the_dangerous_and_the_broken_settings():
    off = startup_warnings(require_for_admins=False, key_configured=True, listener="admin")
    assert len(off) == 1 and "require_2fa_for_admins" in off[0]
    nokey = startup_warnings(require_for_admins=True, key_configured=False, listener="public")
    assert len(nokey) == 1 and "AUTH_SECRET_KEY" in nokey[0]
    assert startup_warnings(require_for_admins=True, key_configured=True, listener="admin") == []
    assert any("localhost" in w for w in startup_warnings(require_for_admins=True, key_configured=True, listener="all"))
