"""
Журнал действий (документ 05, §16; шаг Р-6). Кто, когда, через что и над чем; «было → стало»; причина; хеш адреса.

Правила §16.2–§16.3, обеспеченные здесь и в миграции 0004:
  * запись — только добавление: в PostgreSQL изменение и удаление строк отклоняет триггер (migrations/versions/0004_audit_events.py);
  * секретов в журнале не бывает: записи собираются из структурированных полей; ключи с «password», «token», «secret», «hash»,
    «code», «key» отбрасываются, а строки, похожие на токены и ключи, маскируются (дополнительная страховка);
  * запись добавляется в ту же транзакцию, что и само действие (вызывающий делает commit): нет действия без записи и наоборот.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.db.models import AuditEvent

VIA = ("ui", "api", "import", "assistant", "system", "console")
_SECRET_KEY = re.compile(r"(pass|token|secret|hash|code|key|otp|totp)", re.I)
_SECRET_VALUE = re.compile(r"^[A-Za-z0-9_\-+/=]{24,}$")       # длинная строка без пробелов — похоже на токен или ключ
MAX_TEXT = 300


@dataclass(frozen=True)
class AuditActor:
    """Кто действует: пользователь (id) или система/консоль (None), откуда пришёл запрос (хеш адреса, без самого адреса)."""

    user_id: str | None = None
    via: str = "api"
    ip_hash: str | None = None


SYSTEM = AuditActor(None, "system")
CONSOLE = AuditActor(None, "console")


def _clean_value(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, (list, tuple, set)):
        return [_clean_value(v) for v in (sorted(value) if isinstance(value, set) else value)]
    if isinstance(value, dict):
        return clean(value)
    if isinstance(value, str):
        text = value[:MAX_TEXT]
        return "***" if _SECRET_VALUE.match(text) and not re.fullmatch(r"[0-9a-f-]{36}", text) else text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_TEXT]


def clean(data: dict | None) -> dict | None:
    """Убрать из записи всё, что может быть секретом. id в формате UUID оставляются (это ссылки, а не секреты)."""
    if not data:
        return None
    return {str(k): _clean_value(v) for k, v in data.items() if not _SECRET_KEY.search(str(k))}


def record(db: Session, action: str, *, actor: AuditActor | None = None, target_type: str | None = None,
           target_id: str | None = None, before: dict | None = None, after: dict | None = None, reason: str | None = None,
           store_id: str | None = None) -> AuditEvent:
    """Добавить запись в текущую транзакцию (без commit)."""
    actor = actor or SYSTEM
    event = AuditEvent(at=utcnow(), store_id=store_id, actor_user_id=actor.user_id,
                       via=actor.via if actor.via in VIA else "api", action=action[:64], target_type=target_type,
                       target_id=target_id, before_json=_dump(clean(before)), after_json=_dump(clean(after)),
                       reason=(reason or None) and str(reason)[:MAX_TEXT], ip_hash=actor.ip_hash)
    db.add(event)
    return event


def _dump(data: dict | None) -> str | None:
    return json.dumps(data, ensure_ascii=False, sort_keys=True) if data else None


def changes(before: dict, after: dict) -> tuple[dict, dict]:
    """Только изменившиеся поля «было → стало»."""
    keys = [k for k in after if before.get(k) != after.get(k)]
    return {k: before.get(k) for k in keys}, {k: after.get(k) for k in keys}


def query(db: Session, *, target_id: str | None = None, actor_user_id: str | None = None, action: str | None = None,
          before_at: datetime | None = None, limit: int = 100) -> list[AuditEvent]:
    """Записи новые сверху (фильтры документа 05, §17: по сотруднику, виду действия, периоду)."""
    stmt = select(AuditEvent)
    if target_id:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    if actor_user_id:
        stmt = stmt.where(AuditEvent.actor_user_id == actor_user_id)
    if action:
        stmt = stmt.where(AuditEvent.action.like(f"{action.rstrip('*')}%") if action.endswith("*") else AuditEvent.action == action)
    if before_at:
        stmt = stmt.where(AuditEvent.at < before_at)
    return list(db.execute(stmt.order_by(AuditEvent.at.desc(), AuditEvent.id.desc()).limit(max(1, min(limit, 500)))).scalars())


def as_dict(event: AuditEvent) -> dict:
    return {"id": event.id, "at": event.at, "actor_user_id": event.actor_user_id, "via": event.via, "action": event.action,
            "target_type": event.target_type, "target_id": event.target_id,
            "before": json.loads(event.before_json) if event.before_json else None,
            "after": json.loads(event.after_json) if event.after_json else None, "reason": event.reason}
