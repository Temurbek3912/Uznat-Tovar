"""app/core/account_status.py — состояние учётной записи (документ 05, §12.1, §12.5, §12.7), офлайн."""
from datetime import datetime, timedelta

from app.core import account_status as st

NOW = datetime(2026, 11, 14, 12, 0, 0)


def status(**kw):
    base = dict(is_active=True, archived_at=None, access_until=None, must_change_password=False, now=NOW)
    return st.status_of(**{**base, **kw})


def test_status_order_archive_then_block_then_expiry_then_first_login():
    assert status() == st.ACTIVE
    assert status(must_change_password=True) == st.PENDING
    assert status(access_until=NOW) == st.EXPIRED and status(access_until=NOW + timedelta(seconds=1)) == st.ACTIVE
    assert status(is_active=False, access_until=NOW - timedelta(days=1)) == st.BLOCKED
    assert status(is_active=False, archived_at=NOW) == st.ARCHIVED


def test_sign_in_only_for_active_unarchived_unexpired():
    ok = dict(is_active=True, archived_at=None, access_until=None, now=NOW)
    assert st.can_sign_in(**ok)
    assert not st.can_sign_in(**{**ok, "is_active": False})
    assert not st.can_sign_in(**{**ok, "archived_at": NOW})
    assert not st.can_sign_in(**{**ok, "access_until": NOW})


def test_reason_is_trimmed_cut_and_cleaned_of_control_characters():
    assert st.clean_reason("  уволился \n") == "уволился" and st.clean_reason("   ") is None and st.clean_reason(None) is None
    assert st.clean_reason("a\x00b\x1bc") == "abc" and len(st.clean_reason("я" * 500)) == st.MAX_REASON_LENGTH
