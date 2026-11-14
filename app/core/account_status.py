"""
Состояние учётной записи сотрудника (документ 05, §12.5, §12.7, §12.1 п. 2) — чистая логика без FastAPI и SQLAlchemy.

  active               — работает;
  pending_first_login  — выдан временный пароль, сотрудник ещё не установил свой (§12.2);
  blocked              — заблокирован: вход невозможен, сеансы завершены; можно разблокировать (§12.5);
  expired              — наступила дата «доступ до»: вход невозможен, пока владелец не продлит срок (§12.1);
  archived             — доступ отозван (сотрудник ушёл): запись и история остаются для журнала, вернуть нельзя,
                         логин повторно не используется (§12.7, §12.1 п. 1).

В базе: users.is_active (прежнее поле: False = заблокирован или в архиве), blocked_at, block_reason, archived_at, access_until —
миграция 0003. Порядок проверки — от самого «сильного» состояния.
"""
from __future__ import annotations

from datetime import datetime

ACTIVE, PENDING, BLOCKED, EXPIRED, ARCHIVED = "active", "pending_first_login", "blocked", "expired", "archived"
STATUSES = (ACTIVE, PENDING, BLOCKED, EXPIRED, ARCHIVED)
MAX_REASON_LENGTH = 200


def access_expired(access_until: datetime | None, now: datetime) -> bool:
    """«Доступ до» наступил? Дата хранится в UTC без часового пояса, как остальные времена в базе."""
    return access_until is not None and now >= access_until


def status_of(*, is_active: bool, archived_at: datetime | None, access_until: datetime | None, must_change_password: bool,
              now: datetime) -> str:
    if archived_at is not None:
        return ARCHIVED
    if not is_active:
        return BLOCKED
    if access_expired(access_until, now):
        return EXPIRED
    if must_change_password:
        return PENDING
    return ACTIVE


def can_sign_in(*, is_active: bool, archived_at: datetime | None, access_until: datetime | None, now: datetime) -> bool:
    return archived_at is None and is_active and not access_expired(access_until, now)


def clean_reason(reason: str | None) -> str | None:
    """Причина блокировки или архива: обрезка пробелов и длины; пустая — None. Без управляющих символов (уходит в журнал и интерфейс)."""
    if reason is None:
        return None
    text = "".join(ch for ch in str(reason) if ch == " " or ch.isprintable()).strip()
    return text[:MAX_REASON_LENGTH] or None
