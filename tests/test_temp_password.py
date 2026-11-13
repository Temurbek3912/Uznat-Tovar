"""Временный пароль (документ 05, §12.2): генерация, срок — чистые функции app/core/temp_password.py."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.core import temp_password
from app.core.security import check_password_policy


def test_generated_password_has_the_documented_shape_and_alphabet():
    for _ in range(200):
        value = temp_password.generate()
        assert re.fullmatch(r"[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{4}(-[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{4}){2}", value), value
        assert not set(value) & set("01ILO")                   # без знаков, которые путают при переписывании с листка
        check_password_policy(value)                            # проходит те же правила, что и обычный пароль


def test_generated_passwords_are_random_enough():
    values = {temp_password.generate() for _ in range(2000)}
    assert len(values) == 2000
    assert temp_password.entropy_bits() > 58                    # документ 05: «около 59 бит»


def test_expiry_and_no_expiry():
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    deadline = temp_password.expires_at(now, 24)
    assert deadline == now + timedelta(hours=24)
    assert not temp_password.is_expired(deadline, now + timedelta(hours=23, minutes=59))
    assert temp_password.is_expired(deadline, now + timedelta(hours=24))
    assert temp_password.expires_at(now, 0) is None and temp_password.expires_at(now, None) is None
    assert not temp_password.is_expired(None, now + timedelta(days=3650))     # пароль владельца из .env — без срока


def test_naive_deadline_from_sqlite_is_compared_correctly():
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 11, 8, 11, 0)                       # SQLite возвращает время без пояса
    assert temp_password.is_expired(naive, now)
    assert not temp_password.is_expired(datetime(2026, 11, 8, 13, 0), now)


def test_password_change_pass_is_short_lived():
    assert temp_password.PASS_TTL_MINUTES == 15
