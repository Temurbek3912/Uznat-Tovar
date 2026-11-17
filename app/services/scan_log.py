"""
Сканы покупателей — для помощника владельца «почему не узнал» (решение владельца 2026-11-17; документ 16, §10.5).

Пишется одна строка на ответ покупателю «Узнать товар»: время, итог, способ (штрихкод или вид), товар, а если товара нет в базе —
как программа его назвала. Кадра нет (он и не пишется на диск, D-10а), сведений о покупателе нет (D-10): ни адреса, ни устройства,
ни языка. Строки старше KEEP_DAYS удаляются сами — не чаще раза в час, при очередной записи.

Запись не должна мешать покупателю: любая ошибка здесь — только строка в журнале программы, ответ покупателю уходит как обычно.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from app.core.security import utcnow
from app.db.models import ScanEventORM

logger = logging.getLogger(__name__)

KEEP_DAYS = 90                 # решение владельца 2026-11-17
PURGE_EVERY = 3600.0           # секунд между уборками старых строк
STATUSES = ("matched", "uncertain", "not_in_catalog", "unknown")

_lock = threading.Lock()
_last_purge = 0.0


def _row(result: dict, now: datetime) -> ScanEventORM:
    status = result.get("status")
    status = status if status in STATUSES else "unknown"
    product = result.get("product") if isinstance(result.get("product"), dict) else None
    guess = result.get("guess") if isinstance(result.get("guess"), dict) else {}
    via = result.get("by") if result.get("by") in ("barcode", "look") else None
    return ScanEventORM(
        at=now, status=status, via=via,
        product_id=str(product["id"])[:64] if product and product.get("id") and status in ("matched", "uncertain") else None,
        guess_name=(str(guess.get("name") or "").strip()[:120] or None) if status == "not_in_catalog" else None,
        guess_kind=(str(guess.get("kind") or "").strip()[:32] or None) if status == "not_in_catalog" else None,
    )


def purge_old(db, now: datetime | None = None) -> int:
    """Удалить строки старше KEEP_DAYS. Возвращает, сколько удалено (без commit)."""
    cutoff = (now or utcnow()) - timedelta(days=KEEP_DAYS)
    return db.query(ScanEventORM).filter(ScanEventORM.at < cutoff).delete(synchronize_session=False)


def record(db, result: dict, *, product_id: str | None = None, now: datetime | None = None, clock=time.monotonic) -> None:
    """Записать итог скана. result — ответ покупателю (status, by, product, guess); product_id — id товара, если в публичной
    карточке его нет. Ошибка не поднимается."""
    global _last_purge
    try:
        now = now or utcnow()
        row = _row(result, now)
        if product_id and row.status in ("matched", "uncertain"):
            row.product_id = product_id
        db.add(row)
        with _lock:
            due = clock() - _last_purge >= PURGE_EVERY or _last_purge == 0.0
            if due:
                _last_purge = clock()
        if due:
            purge_old(db, now)
        db.commit()
    except Exception:  # noqa: BLE001 — запись скана необязательна
        logger.exception("Скан покупателя не записан")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
