"""Тесты чистой логики F13 (app/core/security.py): роли, скоуп магазинов, пароли, токены."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.security import (
    ALL_STORES_ROLES,
    ROLE_PERMISSIONS,
    CurrentUser,
    Permission,
    PolicyViolation,
    Role,
    can_access_store,
    canonical_username,
    check_password_policy,
    dummy_password_hash,
    generate_token,
    hash_password,
    hash_token,
    is_safe_path_segment,
    parse_role,
    role_has_permission,
    safe_path_segment,
    token_is_expired,
    utcnow,
    validate_username,
    verify_password,
)

# Люди (не служебные учётки): правила «все видят отчёты/каталог» относятся к ним; роль агента — отдельное исключение.
HUMAN_ROLES = [r for r in Role if r is not Role.CAMERA_AGENT]


class TestRolePermissions:
    def test_permission_matrix(self):
        assert ROLE_PERMISSIONS[Role.MERCHANDISER] == {
            Permission.SNAPSHOT_UPLOAD, Permission.REPORT_READ, Permission.REVIEW_QUEUE, Permission.CATALOG_READ,
        }
        assert ROLE_PERMISSIONS[Role.REGIONAL_MANAGER] == {Permission.REPORT_READ, Permission.CATALOG_READ}
        assert ROLE_PERMISSIONS[Role.ANALYST] == {Permission.REPORT_READ, Permission.CATALOG_READ}
        assert ROLE_PERMISSIONS[Role.ADMIN] == set(Permission)
        assert ROLE_PERMISSIONS[Role.CAMERA_AGENT] == {Permission.SNAPSHOT_UPLOAD, Permission.CAMERA_AGENT}

    def test_every_role_has_an_explicit_entry(self):
        # Новая роль без записи в матрице молча осталась бы без прав — ловим это тестом.
        assert set(ROLE_PERMISSIONS) == set(Role)

    def test_only_admin_manages_catalog_and_users(self):
        for role in Role:
            expected = role is Role.ADMIN
            assert role_has_permission(role, Permission.CATALOG_WRITE) is expected
            assert role_has_permission(role, Permission.USER_MANAGE) is expected

    def test_everyone_can_read_the_catalog(self):
        # В отличие от CATALOG_WRITE — просмотр/поиск SKU не меняет ничего и не ограничен так же строго.
        assert all(role_has_permission(r, Permission.CATALOG_READ) for r in HUMAN_ROLES)

    def test_only_merchandiser_and_admin_upload(self):
        uploaders = {r for r in Role if role_has_permission(r, Permission.SNAPSHOT_UPLOAD)}
        assert uploaders == {Role.MERCHANDISER, Role.ADMIN, Role.CAMERA_AGENT}      # агент камер — служебная роль, см. test_camera_agent_role_*

    def test_only_merchandiser_and_admin_review_queue(self):
        # F12: надзорные роли (regional_manager, analyst) явно ИСКЛЮЧЕНЫ — см. комментарий
        # в ROLE_PERMISSIONS про операционный, а не управленческий характер решения.
        reviewers = {r for r in Role if role_has_permission(r, Permission.REVIEW_QUEUE)}
        assert reviewers == {Role.MERCHANDISER, Role.ADMIN}

    def test_everyone_can_read_reports(self):
        assert all(role_has_permission(r, Permission.REPORT_READ) for r in HUMAN_ROLES)

    def test_none_role_has_no_permissions(self):
        assert not any(role_has_permission(None, p) for p in Permission)

    def test_parse_role(self):
        assert parse_role("admin") is Role.ADMIN
        assert parse_role(Role.ANALYST) is Role.ANALYST
        assert parse_role("superuser") is None
        assert parse_role("") is None
        assert parse_role(None) is None


class TestStoreScope:
    def test_all_stores_roles(self):
        assert ALL_STORES_ROLES == {Role.ANALYST, Role.ADMIN}
        for role in ALL_STORES_ROLES:
            assert can_access_store(role, [], "any-store")

    def test_scoped_roles_only_see_assigned_stores(self):
        for role in (Role.MERCHANDISER, Role.REGIONAL_MANAGER):
            assert can_access_store(role, {"s1", "s2"}, "s1")
            assert not can_access_store(role, {"s1", "s2"}, "s3")

    def test_scoped_role_without_assignments_sees_nothing(self):
        assert not can_access_store(Role.MERCHANDISER, set(), "s1")

    def test_none_role_sees_nothing(self):
        assert not can_access_store(None, {"s1"}, "s1")

    def test_current_user_delegates(self):
        merch = CurrentUser(id="u1", username="m", role=Role.MERCHANDISER, store_ids=frozenset({"s1"}))
        assert merch.has_permission(Permission.SNAPSHOT_UPLOAD)
        assert not merch.has_permission(Permission.CATALOG_WRITE)
        assert merch.can_access_store("s1")
        assert not merch.can_access_store("s2")

        analyst = CurrentUser(id="u2", username="a", role=Role.ANALYST)
        assert analyst.can_access_store("anything")
        assert not analyst.has_permission(Permission.SNAPSHOT_UPLOAD)


class TestPasswords:
    def test_roundtrip(self):
        h = hash_password("correct horse battery")
        assert verify_password("correct horse battery", h)

    def test_wrong_password_rejected(self):
        h = hash_password("correct horse battery")
        assert not verify_password("correct horse batterY", h)
        assert not verify_password("", h)

    def test_unique_salt_per_hash(self):
        assert hash_password("same-password-1") != hash_password("same-password-1")

    def test_hash_does_not_contain_plaintext(self):
        assert "hunter2hunter2" not in hash_password("hunter2hunter2")

    def test_unicode_password(self):
        h = hash_password("парольТашкент2026")
        assert verify_password("парольТашкент2026", h)
        assert not verify_password("парольТашкент2025", h)

    def test_malformed_hash_returns_false_not_exception(self):
        for bad in ("", "garbage", "scrypt$1$2", "bcrypt$a$b$c$d$e", "scrypt$x$8$1$AAAA$AAAA",
                    "scrypt$16384$8$1$!!!notb64!!!$AAAA"):
            assert verify_password("whatever-password", bad) is False

    def test_overlong_password_rejected_without_hashing(self):
        h = hash_password("a-normal-password")
        assert verify_password("x" * 10_000, h) is False

    def test_stored_parameters_are_honoured(self):
        # Хеш, посчитанный с другими параметрами (как будто N повысили позже), должен проверяться.
        import base64
        import hashlib

        salt = b"0123456789abcdef"
        digest = hashlib.scrypt(b"legacy-password", salt=salt, n=2**12, r=8, p=1, dklen=32)
        stored = "scrypt$4096$8$1${}${}".format(
            base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
        )
        assert verify_password("legacy-password", stored)
        assert not verify_password("other-password", stored)

    def test_dummy_hash_is_valid_but_matches_nothing_guessable(self):
        h = dummy_password_hash()
        assert h.startswith("scrypt$")
        assert dummy_password_hash() == h  # закеширован — не пересчитывается на каждый логин
        assert not verify_password("password", h)

    def test_policy(self):
        check_password_policy("12345678")
        with pytest.raises(PolicyViolation):
            check_password_policy("1234567")
        with pytest.raises(PolicyViolation):
            check_password_policy("x" * 129)


class TestUsername:
    def test_canonical(self):
        assert canonical_username("  Ivan.Petrov ") == "ivan.petrov"
        assert canonical_username("") == ""

    def test_valid(self):
        assert validate_username("Admin") == "admin"
        assert validate_username("m.karimov@shop-1") == "m.karimov@shop-1"

    def test_invalid(self):
        for bad in ("", "ab", "x" * 65, "has space", "-leading", "кириллица", "semi;colon", "a/b"):
            with pytest.raises(PolicyViolation):
                validate_username(bad)


class TestTokens:
    def test_tokens_are_unique_and_long(self):
        a, b = generate_token(), generate_token()
        assert a != b
        assert len(a) >= 40

    def test_hash_is_deterministic_and_not_the_token(self):
        t = generate_token()
        assert hash_token(t) == hash_token(t)
        assert hash_token(t) != t
        assert len(hash_token(t)) == 64
        assert hash_token(t) != hash_token(generate_token())

    def test_expiry(self):
        now = utcnow()
        assert not token_is_expired(now + timedelta(minutes=1), now)
        assert token_is_expired(now - timedelta(seconds=1), now)
        assert token_is_expired(now, now)  # граница: в момент expires_at токен уже недействителен


class TestPathSegments:
    def test_normal_ids_are_safe(self):
        assert is_safe_path_segment("shelf-demo-1")
        assert is_safe_path_segment("3f2b8c1e-5d4a-4c1b-9a7e-0123456789ab")

    def test_traversal_and_separators_are_unsafe(self):
        for bad in ("..", ".", "../../etc", "a/b", "a\\b", "/abs", ".hidden", "", " ", "a..b", "x" * 200, "полка-1"):
            assert not is_safe_path_segment(bad), bad

    def test_safe_path_segment_keeps_good_values(self):
        assert safe_path_segment("shelf-demo-1") == "shelf-demo-1"

    def test_safe_path_segment_neutralises_bad_values_deterministically(self):
        for bad in ("../../../etc", "a/b", "полка-1", ""):
            out = safe_path_segment(bad)
            assert is_safe_path_segment(out), (bad, out)
            assert out == safe_path_segment(bad)  # детерминированно: одна полка -> одна папка
        assert safe_path_segment("полка-1") != safe_path_segment("полка-2")


# ---------------------------------------------------------------------------------------------------------
# Камеры и служебная роль агента (админ-панель, camera_agent --managed)
# ---------------------------------------------------------------------------------------------------------

def test_camera_agent_role_can_only_upload_snapshots_and_read_camera_list():
    perms = ROLE_PERMISSIONS[Role.CAMERA_AGENT]
    assert perms == {Permission.SNAPSHOT_UPLOAD, Permission.CAMERA_AGENT}
    for forbidden in (Permission.REPORT_READ, Permission.CATALOG_READ, Permission.CATALOG_WRITE, Permission.USER_MANAGE,
                      Permission.STORE_MANAGE, Permission.CAMERA_MANAGE, Permission.REVIEW_QUEUE):
        assert not role_has_permission(Role.CAMERA_AGENT, forbidden), forbidden


def test_only_admin_manages_cameras_and_only_agent_and_admin_use_the_agent_api():
    managers = {r for r in Role if role_has_permission(r, Permission.CAMERA_MANAGE)}
    assert managers == {Role.ADMIN}
    agents = {r for r in Role if role_has_permission(r, Permission.CAMERA_AGENT)}
    assert agents == {Role.ADMIN, Role.CAMERA_AGENT}


def test_camera_agent_sees_only_assigned_stores_not_the_whole_network():
    assert Role.CAMERA_AGENT not in ALL_STORES_ROLES
    user = CurrentUser(id="u", username="agent", role=Role.CAMERA_AGENT, store_ids=frozenset({"A"}))
    assert user.can_access_store("A") and not user.can_access_store("B")
