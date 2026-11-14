"""app/services/audit.py — что попадает в журнал (документ 05, §16.2): ничего похожего на секреты."""
from datetime import datetime

from app.services import audit


def test_secret_like_keys_are_dropped_and_token_like_values_masked():
    data = {"password": "x", "new_password": "y", "token": "t", "totp_secret_enc": "s", "recovery_code": "c", "api_key": "k",
            "password_hash": "h", "role": "admin", "note": "A" * 40, "id": "0b8e2f6a-1c3d-4e5f-8a9b-0c1d2e3f4a5b",
            "when": datetime(2026, 11, 14, 12, 0), "stores": {"b", "a"}}
    out = audit.clean(data)
    assert set(out) == {"role", "note", "id", "when", "stores"}
    assert out["note"] == "***" and out["id"].startswith("0b8e2f6a") and out["when"] == "2026-11-14T12:00:00" and out["stores"] == ["a", "b"]
    assert audit.clean({}) is None and audit.clean(None) is None


def test_changes_keep_only_what_changed():
    assert audit.changes({"role": "a", "x": 1}, {"role": "b", "x": 1}) == ({"role": "a"}, {"role": "b"})
    assert audit.changes({"a": 1}, {"a": 1}) == ({}, {})
