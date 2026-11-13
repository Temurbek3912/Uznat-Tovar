"""Админ-панель: безопасные настройки, место на диске, время и часовой пояс, правила защиты от удаления магазинов и полок."""
from __future__ import annotations

import types
from datetime import datetime

import pytest

from app.services import admin_service as adm
from app.services import store_service as ss

NOW = datetime(2026, 10, 10, 12, 0, 0)


# ------------------------------------------------------------------ настройки: секреты наружу не попадают

def _settings(**kw):
    base = dict(environment="development", app_timezone="Asia/Tashkent", max_upload_mb=25, auth_token_ttl_minutes=720,
                auth_max_failed_logins_per_user=5, auth_max_failed_logins_per_ip=20, auth_lockout_window_minutes=15,
                media_root="data", secret_key="SUPER-SECRET-KEY-VALUE", database_url="postgresql://u:DBPASS@h/x",
                queue_broker_url="redis://:REDISPASS@r/0", detector=types.SimpleNamespace(confidence_threshold=0.25),
                matcher=types.SimpleNamespace(margin_threshold=0.1))
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_settings_view_never_exposes_keys_passwords_or_urls_and_is_read_only():
    items = adm.settings_view(_settings())
    text = str(items)
    assert "SUPER-SECRET" not in text and "DBPASS" not in text and "REDISPASS" not in text and "postgresql" not in text
    assert all(i["editable"] is False for i in items)
    assert "camera_secret_key" not in {i["key"] for i in items}          # ключа камер в настройках больше нет


def test_settings_view_skips_absent_values():
    items = adm.settings_view(_settings(detector=None, matcher=None))
    keys = {i["key"] for i in items}
    assert "detector_confidence" not in keys and "matcher_margin" not in keys and "environment" in keys
    assert "osa_empty_slot_confidence" not in {i["key"] for i in adm.settings_view(_settings())}      # настроек аудита в списке нет


# ------------------------------------------------------------------ диск

def test_dir_usage_counts_bytes_and_files_and_caps_the_walk(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "1.jpg").write_bytes(b"x" * 100)
    (tmp_path / "2.jpg").write_bytes(b"y" * 50)
    assert adm.dir_usage(tmp_path) == {"exists": True, "bytes": 150, "files": 2, "truncated": False}
    capped = adm.dir_usage(tmp_path, max_files=1)
    assert capped["truncated"] is True and capped["files"] == 1
    assert adm.dir_usage(tmp_path / "nope") == {"exists": False, "bytes": 0, "files": 0, "truncated": False}


def test_disk_free_works_for_a_not_yet_existing_folder(tmp_path):
    info = adm.disk_free(tmp_path / "not" / "created" / "yet")
    assert info is not None and info["free_bytes"] > 0 and info["total_bytes"] >= info["free_bytes"]


# ------------------------------------------------------------------ время и часовой пояс (перенесено из удалённых camera_service и daily_report)

def test_iso_utc_marks_naive_utc_time_with_z_and_keeps_none():
    assert adm.iso_utc(datetime(2026, 10, 10, 11, 55, 0, 123456)) == "2026-10-10T11:55:00Z"
    assert adm.iso_utc(None) is None


def test_fixed_offset_fallback_works_without_tz_database():
    import zoneinfo
    original = zoneinfo.ZoneInfo

    def broken(name):
        raise zoneinfo.ZoneInfoNotFoundError(name)
    zoneinfo.ZoneInfo = broken
    try:
        tz = adm.tz_for("Asia/Tashkent")
        assert tz.utcoffset(None).total_seconds() == 5 * 3600
        with pytest.raises(ValueError):
            adm.tz_for("Europe/Paris")                                   # для остальных — явная ошибка, а не молчаливое UTC
    finally:
        zoneinfo.ZoneInfo = original


# ------------------------------------------------------------------ защита от удаления

def test_shelf_with_snapshots_or_cameras_cannot_be_deleted_and_planogram_needs_confirmation():
    assert ss.shelf_delete_blockers(0, 0, 0, False) == []
    assert len(ss.shelf_delete_blockers(5, 1, 0, False)) == 2
    assert "снимки (5)" in ss.shelf_delete_blockers(5, 0, 0, True)[0]                  # подтверждение планограммы не снимает запрет из-за снимков
    assert len(ss.shelf_delete_blockers(0, 0, 12, False)) == 1 and ss.shelf_delete_blockers(0, 0, 12, True) == []


def test_store_is_deletable_only_when_empty_and_all_reasons_are_listed():
    assert ss.store_delete_blockers(0, 0, 0, 0) == []
    reasons = ss.store_delete_blockers(3, 2, 7, 1)
    assert len(reasons) == 4 and "полки (3)" in reasons[0] and "пользователи (1)" in reasons[3]
