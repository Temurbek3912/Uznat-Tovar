"""
Админ-панель: чистая логика без БД/FastAPI (tests/test_admin_service.py) — безопасное представление настроек, место на диске,
время и часовой пояс. Запросы к БД — в app/api/routes_admin.py.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

DEFAULT_TIMEZONE = "Asia/Tashkent"


def iso_utc(value: datetime | None) -> str | None:
    """Наивное UTC-время из БД -> ISO-строка с «Z», чтобы браузер разбирал её как UTC, а не как местное время."""
    return None if value is None else value.replace(microsecond=0).isoformat() + "Z"


def tz_for(name: str | None) -> tzinfo:
    """Часовой пояс по имени. На Windows без пакета tzdata zoneinfo не находит базу — для Узбекистана (UTC+5, без летнего
    времени с 1992 года) есть надёжный запасной вариант; для других имён без базы — явная ошибка, а не молчаливое UTC."""
    wanted = (name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(wanted)
    except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError и др.: базы часовых поясов нет в системе
        if wanted in ("Asia/Tashkent", "Asia/Samarkand"):
            return timezone(timedelta(hours=5), "Asia/Tashkent")
        raise ValueError(f"Часовой пояс {wanted!r} недоступен: установите пакет tzdata (pip install tzdata) или используйте Asia/Tashkent") from None


def settings_view(settings) -> list[dict]:
    """Только безопасные, реально используемые приложением параметры. Секреты (ключи, пароли, URL с паролем) сюда не попадают.
    Все значения — только для чтения: меняются в configs/config.yaml или .env (и применяются перезапуском) —
    произвольные переменные окружения через веб не редактируются."""
    def get(obj, name, default=None):
        return getattr(obj, name, default) if obj is not None else default

    detector, matcher = get(settings, "detector"), get(settings, "matcher")
    items = [
        ("environment", "Режим работы", get(settings, "environment"), ".env: ENVIRONMENT (production отключает /docs)"),
        ("app_timezone", "Часовой пояс", get(settings, "app_timezone"), ".env: APP_TIMEZONE"),
        ("max_upload_mb", "Макс. размер фото, МБ", get(settings, "max_upload_mb"), ".env: MAX_UPLOAD_MB"),
        ("auth_token_ttl_minutes", "Срок жизни токена входа, мин", get(settings, "auth_token_ttl_minutes"), "config.yaml: auth.token_ttl_minutes"),
        ("auth_max_failed_logins_per_user", "Неудачных входов на логин до блокировки", get(settings, "auth_max_failed_logins_per_user"), "config.yaml: auth"),
        ("auth_max_failed_logins_per_ip", "Неудачных входов на IP до блокировки", get(settings, "auth_max_failed_logins_per_ip"), "config.yaml: auth"),
        ("auth_lockout_window_minutes", "Окно блокировки входа, мин", get(settings, "auth_lockout_window_minutes"), "config.yaml: auth"),
        ("media_root", "Папка фото", get(settings, "media_root"), ".env / config.yaml: app.media_root"),
        ("detector_confidence", "Порог уверенности детектора", get(detector, "confidence_threshold"), "config.yaml: models.detector"),
        ("matcher_margin", "Запас различимости матчера", get(matcher, "margin_threshold"), "config.yaml: models.matcher (подбирается ml.evaluate_matching)"),
        ("matcher_twin", "Порог двойника (похожие фото в каталоге -> «похоже»)", get(matcher, "twin_threshold"), "config.yaml: models.matcher (ml.check_twins)"),
        ("matcher_strict", "Строгий режим «Узнать товар»", get(matcher, "strict"), "config.yaml: models.matcher.strict"),
    ]
    return [{"key": k, "label": label, "value": v, "source": src, "editable": False} for k, label, v, src in items if v is not None]


def dir_usage(path, max_files: int = 200_000) -> dict:
    """Сколько места занимают файлы в папке. Обход ограничен max_files — на огромной папке интерфейс не должен виснуть."""
    root = Path(path)
    total, count, truncated = 0, 0, False
    if not root.is_dir():
        return {"exists": False, "bytes": 0, "files": 0, "truncated": False}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if count >= max_files:
                return {"exists": True, "bytes": total, "files": count, "truncated": True}
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
            count += 1
    return {"exists": True, "bytes": total, "files": count, "truncated": truncated}


def disk_free(path) -> dict | None:
    try:
        target = Path(path)
        while not target.exists() and target != target.parent:
            target = target.parent
        usage = shutil.disk_usage(target)
        return {"total_bytes": usage.total, "free_bytes": usage.free}
    except OSError:
        return None
