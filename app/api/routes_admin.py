"""
Данные для админ-панели (frontend/admin.html). Только администратор (право store:manage). Все цифры — из БД и реального
состояния системы; вымышленных показателей нет. Если данных нет, так и сказано (None / пустые списки), а не 0% и не 100%.

  GET /api/v1/admin/system                       состояние: БД, очередь, воркер, диск, настройки (только чтение)
  GET /api/v1/admin/products?q=&limit=&offset=   каталог товаров для таблицы «Товары»
  GET /api/v1/admin/shop-card                    какие поля карточки видит покупатель в этом магазине
  PUT /api/v1/admin/shop-card                    выбрать поля ({"fields": [...]}); срок годности включить нельзя (журнал действий)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.api.deps import require_permission
from app.core import text_search
from app.core.config import get_settings, resolve_project_path
from app.core.security import CurrentUser, Permission
from app.core.errors import ApiError
from app.db.models import Product, Store
from app.db.session import get_db
from app.services import audit, shop_card
from app.services.admin_service import (
    dir_usage,
    disk_free,
    iso_utc,
    settings_view,
    tz_for,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
_admin = require_permission(Permission.STORE_MANAGE)


def _queue_state() -> str:
    if get_settings().lite_mode:      # облегчённый режим: очереди нет — задачи выполняются в процессе API
        return "ok"
    try:
        import redis
        redis.Redis.from_url(get_settings().queue_broker_url, socket_connect_timeout=2, socket_timeout=2).ping()
        return "ok"
    except Exception as exc:  # noqa: BLE001 — класс ошибки — в журнал, клиенту только «fail» (единая система ошибок)
        logger.warning("Админ-панель: очередь недоступна (%s)", type(exc).__name__)
        return "fail"


def _worker_state() -> dict:
    """Celery-воркер отвечает на ping по брокеру. Ответа нет за секунду — «не отвечает» (а не «упал»: он может грузить модель)."""
    if get_settings().lite_mode:      # облегчённый режим: «воркер» — сам процесс API
        from app.pipeline import ml_runtime
        status = ml_runtime.read_status() or {}
        return {"online": bool(status.get("ready")), "nodes": ["в процессе API (облегчённый режим)"]}
    try:
        from app.worker import celery_app
        replies = celery_app.control.ping(timeout=1.0) or []
        return {"online": bool(replies), "nodes": sorted(name for reply in replies for name in reply)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Админ-панель: опрос воркера не удался (%s)", type(exc).__name__)
        return {"online": False, "nodes": []}


@router.get("/system")
def system(db: Session = Depends(get_db), _user: CurrentUser = Depends(_admin)) -> dict:
    settings = get_settings()
    now = datetime.utcnow()
    tz = tz_for(settings.app_timezone)
    media = resolve_project_path(settings.media_root)
    try:
        db.query(func.count(Product.id)).scalar()
        database = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Админ-панель: база данных не отвечает (%s)", type(exc).__name__)
        database = "fail"
    return {
        "server_time_utc": iso_utc(now), "timezone": settings.app_timezone,
        "server_time_local": now.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
        "database": database, "queue": _queue_state(), "worker": _worker_state(),
        "storage": {"path": str(settings.media_root), "usage": dir_usage(media), "disk": disk_free(media)},
        "settings": settings_view(settings),
        "settings_note": "Параметры только для чтения: меняются в configs/config.yaml или .env и применяются перезапуском "
                         "(docker compose up -d). Произвольные переменные окружения через веб не редактируются.",
    }


@router.get("/products")
def products(q: str = "", limit: int = 50, offset: int = 0, db: Session = Depends(get_db),
             _user: CurrentUser = Depends(_admin)) -> dict:
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    query = db.query(Product)
    pattern = text_search.like_pattern(q)            # как поиск каталога: регистр, ё/е, апострофы; % и _ — обычные символы
    if pattern:
        query = query.filter(or_(*(text_search.sql_normalized(col, func).like(pattern, escape=text_search.ESCAPE)
                                   for col in (Product.sku, Product.name, Product.brand))))
    total = query.count()
    page = query.order_by(Product.sku).offset(offset).limit(limit).all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [{"id": p.id, "sku": p.sku, "name": p.name, "brand": p.brand, "category": p.category,
                       "expected_price": p.expected_price} for p in page]}


class CardFieldsIn(BaseModel):
    fields: list[str]


def _store(db: Session) -> Store | None:
    return db.query(Store).order_by(Store.name).first()      # одна запись Store — «этот магазин» (как у входа покупателя)


def _card_payload(store: Store | None) -> dict:
    return {"fields": list(shop_card.store_fields(store)), "available": list(shop_card.CARD_FIELDS),
            "default": list(shop_card.DEFAULT_FIELDS), "always": ["name", "brand", "category"], "store": store.name if store else None,
            "expiry_note": shop_card.EXPIRY_REFUSAL}


@router.get("/shop-card")
def get_shop_card(db: Session = Depends(get_db), _user: CurrentUser = Depends(_admin)) -> dict:
    """Поля карточки покупателя: выбранные (fields), все возможные (available), по умолчанию (default). Название, бренд и
    категория видны всегда. Срока годности в списке нет и не будет (app/services/shop_card.py)."""
    return _card_payload(_store(db))


@router.put("/shop-card")
def put_shop_card(body: CardFieldsIn, db: Session = Depends(get_db), user: CurrentUser = Depends(_admin)) -> dict:
    try:
        fields = shop_card.validate_fields(body.fields)
    except shop_card.CardFieldsError as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    store = _store(db)
    if store is None:
        # Записи магазина ещё нет (магазины в управлении не заводятся: одна программа — один магазин): создаём её без названия —
        # на странице покупателя пустое название не показывается. Иначе настройку было бы некуда сохранить.
        store = Store(name="", address="", format="supermarket")
        db.add(store)
        db.flush()
    before = list(shop_card.store_fields(store))
    store.card_fields = shop_card.dump_fields(fields)
    if before != list(fields):
        audit.record(db, "store.card_fields_changed", actor=audit.AuditActor(user.id, "api"), target_type="store",
                     target_id=store.id, store_id=store.id, before={"fields": before}, after={"fields": list(fields)})
    db.commit()
    db.refresh(store)
    return _card_payload(store)
