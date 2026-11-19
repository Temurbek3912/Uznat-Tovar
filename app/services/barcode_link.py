"""
Привязка штрих-кодов к только что созданному товару (воркер, задача «Добавить товар») и сообщение человеку о неудаче.

Зачем отдельно. API проверяет, свободен ли код, ДО постановки задачи в очередь, но пока задача ждёт и изучает фото, другой запрос
может успеть занять тот же код (например, привязать его к другому товару в карточке). Товар к этому моменту уже сохранён и
проиндексирован, поэтому сбой привязки НЕ откатывает добавление. Раньше о нём знал только журнал воркера; теперь задача
возвращает список непривязанных кодов, и страница «Товары» и помощник показывают: «код … не привязан: он уже у товара …».

Повтор задачи (Celery retry) снова привязывает те же коды: код, который уже у ЭТОГО товара, — не ошибка.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.db.models import Product, ProductBarcodeORM

logger = logging.getLogger(__name__)


def _owner_of(db: Session, code: str) -> tuple[str, str] | None:
    """(id товара, название) владельца кода или None (код свободен или база не ответила)."""
    try:
        row = (db.query(ProductBarcodeORM.product_id, Product.name)
               .outerjoin(Product, Product.id == ProductBarcodeORM.product_id)
               .filter(ProductBarcodeORM.barcode == code).first())
    except Exception:  # noqa: BLE001 — сообщение о неудаче не должно само ронять задачу
        db.rollback()
        return None
    return (row[0], row[1] or "") if row else None


def link_barcodes(db: Session, product_id: str, barcodes: list[str]) -> list[dict]:
    """Привязывает коды к товару по одному (каждый — своя фиксация). Возвращает непривязанные: [{"code", "name"}], где name —
    название товара, у которого код уже есть ("" — владелец не найден: ошибка базы). Исключения не пробрасываются."""
    not_linked: list[dict] = []
    for code in barcodes:
        try:
            db.add(ProductBarcodeORM(barcode=code, product_id=product_id))
            db.commit()
            continue
        except Exception:  # noqa: BLE001 — IntegrityError: код уже занят; любая иная — тоже не повод терять товар
            db.rollback()
        owner = _owner_of(db, code)
        if owner is not None and owner[0] == product_id:
            continue                       # повтор задачи: код уже привязан к этому же товару
        not_linked.append({"code": code, "name": owner[1] if owner else ""})
        logger.warning("Штрих-код %s не привязан к товару %s (%s)", code, product_id,
                       f"уже у товара {owner[0]}" if owner else "ошибка БД")
    return not_linked


def task_result(product_id: str, not_linked: list[dict]) -> str | dict:
    """Итог задачи «Добавить товар»: как прежде — просто id товара; если какие-то коды не привязаны — словарь с их списком."""
    return {"product_id": product_id, "barcodes_not_linked": not_linked} if not_linked else product_id
