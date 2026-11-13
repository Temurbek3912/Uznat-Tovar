"""
GET  /api/v1/stores                    — магазины в скоупе пользователя и их полки.
POST /api/v1/stores                    — создать магазин (+ полки сразу, пачкой)         (только admin, право store:manage)
POST /api/v1/stores/{store_id}/shelves — добавить полки в магазин, до 500 за запрос       (только admin)
PATCH /api/v1/stores/{store_id}        — изменить название/адрес/формат                    (только admin)
PATCH /api/v1/stores/{store_id}/shelves/{shelf_code}   — переименовать полку (код не меняется: на нём держатся снимки и планограмма)
DELETE /api/v1/stores/{store_id}/shelves/{shelf_code}  — удалить полку; ЗАПРЕЩЕНО, если есть снимки или камеры; планограмму — только
                                                         с ?with_planogram=true
DELETE /api/v1/stores/{store_id}       — удалить ПУСТОЙ магазин (без полок, камер, снимков и закреплённых пользователей)

Создание магазинов раньше было возможно только CLI-скриптом (scripts/setup_store.py), поэтому свежеустановленная
система показывала пустой список магазинов и админу было нечем его заполнить. Страница frontend/setup.html
использует эти два POST. Код полки глобально уникален (планограмма ключуется одним shelf_id) — если не задан,
генерируется как «<магазин>-<полка>» (app/services/store_service.py:plan_shelves).


Нужен клиентам, которые загружают снимки (телефон, агент камер): без него они не знали бы, какие
store_id/shelf_id подставить. Любая роль с правом чтения отчётов видит ТОЛЬКО свои магазины
(admin/analyst — все), чужие не раскрываются.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ApiError, from_app_error, reason
from app.api.deps import require_permission, require_recent_auth
from app.core.security import CurrentUser, Permission
from app.db.models import CameraORM, PlanogramSlotORM, ShelfORM, ShelfSnapshot, Store, UserStoreAccess
from app.db.session import get_db
from app.services.store_service import (
    MAX_SHELVES_PER_REQUEST,
    MAX_NAME_LENGTH,
    StoreInputError,
    build_store_listing,
    plan_shelves,
    shelf_delete_blockers,
    store_delete_blockers,
    validate_store_fields,
)

def _shelf_race() -> ApiError:
    """Гонка двух запросов за один код полки (сработал уникальный индекс): тот же текст, что раньше, код — для перевода."""
    return ApiError(409, "shelf_code_race", "Код полки уже занят — повторите запрос")

router = APIRouter(prefix="/api/v1/stores", tags=["stores"])


# def, а не async def: запросы к БД синхронные (см. комментарий у crop.jpg в routes_review.py).
@router.get("")
def list_stores(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.REPORT_READ)),
) -> list[dict]:
    stores = [s for s in db.query(Store).all() if user.can_access_store(s.id)]
    if not stores:
        return []
    store_ids = [s.id for s in stores]

    shelves = db.query(ShelfORM).filter(ShelfORM.store_id.in_(store_ids)).all()
    pairs = (
        db.query(ShelfSnapshot.store_id, ShelfSnapshot.shelf_id)
        .filter(ShelfSnapshot.store_id.in_(store_ids))
        .distinct()
        .all()
    )
    slot_counts = dict(
        db.query(PlanogramSlotORM.shelf_id, func.count(PlanogramSlotORM.id))
        .group_by(PlanogramSlotORM.shelf_id).all()
    )
    return build_store_listing(stores, shelves, [(a, b) for a, b in pairs], slot_counts)


# ------------------------------------------------------------------------------------ создание магазинов и полок


class ShelfIn(BaseModel):
    name: str
    shelf_id: str | None = None      # необязательный код; пусто -> генерируется из названий магазина и полки


class StoreCreate(BaseModel):
    name: str
    address: str = ""                # в БД колонка NOT NULL, но для работы адрес не нужен — пустая строка допустима
    format: str = "supermarket"      # hypermarket | supermarket | convenience
    shelves: list[ShelfIn] = Field(default_factory=list, max_length=MAX_SHELVES_PER_REQUEST)


class ShelvesAdd(BaseModel):
    shelves: list[ShelfIn] = Field(min_length=1, max_length=MAX_SHELVES_PER_REQUEST)


def _store_payload(db: Session, store: Store) -> dict:
    """Магазин в том же виде, что элементы GET /api/v1/stores (shelves, planogram_slots)."""
    shelves = db.query(ShelfORM).filter(ShelfORM.store_id == store.id).all()
    codes = [sh.shelf_id for sh in shelves]
    slot_counts = {}
    if codes:
        slot_counts = dict(
            db.query(PlanogramSlotORM.shelf_id, func.count(PlanogramSlotORM.id))
            .filter(PlanogramSlotORM.shelf_id.in_(codes)).group_by(PlanogramSlotORM.shelf_id).all()
        )
    return build_store_listing([store], shelves, [], slot_counts)[0]


def _add_shelves(db: Session, store: Store, items: list[ShelfIn]) -> None:
    """Добавляет полки в сессию (без commit). StoreInputError — текст для пользователя."""
    taken = {code for (code,) in db.query(ShelfORM.shelf_id).all()}
    planned = plan_shelves([(sh.shelf_id, sh.name) for sh in items], taken, store.name)
    for code, name in planned:
        db.add(ShelfORM(store_id=store.id, shelf_id=code, name=name))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_store(
    body: StoreCreate,
    db: Session = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission(Permission.STORE_MANAGE)),
) -> dict:
    try:
        name, address, fmt = validate_store_fields(body.name, body.address, body.format)
        store = Store(name=name, address=address, format=fmt)
        db.add(store)
        db.flush()                               # нужен store.id для полок; до commit ничего не видно другим
        _add_shelves(db, store, body.shelves)
        db.commit()
    except StoreInputError as exc:
        db.rollback()
        raise from_app_error(exc) from exc
    except IntegrityError:
        # Гонка: другой запрос успел занять тот же код полки между проверкой и INSERT (unique-индекс сработал).
        db.rollback()
        raise _shelf_race() from None
    db.refresh(store)
    return _store_payload(db, store)


@router.post("/{store_id}/shelves", status_code=status.HTTP_201_CREATED)
def add_shelves(
    store_id: str,
    body: ShelvesAdd,
    db: Session = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission(Permission.STORE_MANAGE)),
) -> dict:
    store = db.get(Store, store_id)
    if store is None:
        raise ApiError(404, "store_not_found", "Магазин не найден")
    try:
        _add_shelves(db, store, body.shelves)
        db.commit()
    except StoreInputError as exc:
        db.rollback()
        raise from_app_error(exc) from exc
    except IntegrityError:
        db.rollback()
        raise _shelf_race() from None
    return _store_payload(db, store)


# ------------------------------------------------------------------------------------ правка и защищённое удаление


class StorePatch(BaseModel):
    name: str | None = None
    address: str | None = None
    format: str | None = None


class ShelfPatch(BaseModel):
    name: str


def _conflict(reasons: list[str]) -> ApiError:
    """Удаление запрещено: причины — по-русски в detail (как раньше) и списком вложенных причин {key, params} в параметрах —
    страница переведёт каждую и соединит через «; ». Причина без кода (строка) уходит как есть."""
    items = [reason(getattr(r, "code", None), getattr(r, "params", None)) or str(r) for r in reasons]
    return ApiError(409, "delete_blocked", "Удалить нельзя: " + "; ".join(reasons), {"reasons": items})


@router.patch("/{store_id}")
def update_store(
    store_id: str,
    body: StorePatch,
    db: Session = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission(Permission.STORE_MANAGE)),
) -> dict:
    store = db.get(Store, store_id)
    if store is None:
        raise ApiError(404, "store_not_found", "Магазин не найден")
    try:
        name, address, fmt = validate_store_fields(
            body.name if body.name is not None else store.name,
            body.address if body.address is not None else store.address,
            body.format if body.format is not None else store.format)
    except StoreInputError as exc:
        raise from_app_error(exc) from exc
    store.name, store.address, store.format = name, address, fmt
    db.commit()
    db.refresh(store)
    return _store_payload(db, store)


def _get_shelf(db: Session, store_id: str, shelf_code: str) -> ShelfORM:
    shelf = db.query(ShelfORM).filter(ShelfORM.shelf_id == shelf_code, ShelfORM.store_id == store_id).first()
    if shelf is None:
        raise ApiError(404, "shelf_not_found", "Полка не найдена в этом магазине")
    return shelf


@router.patch("/{store_id}/shelves/{shelf_code}")
def rename_shelf(
    store_id: str,
    shelf_code: str,
    body: ShelfPatch,
    db: Session = Depends(get_db),
    _admin: CurrentUser = Depends(require_permission(Permission.STORE_MANAGE)),
) -> dict:
    shelf = _get_shelf(db, store_id, shelf_code)
    name = (body.name or "").strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        raise ApiError(422, "shelf_name_length", f"Название полки: от 1 до {MAX_NAME_LENGTH} символов", {"max": MAX_NAME_LENGTH})
    shelf.name = name
    db.commit()
    return _store_payload(db, db.get(Store, store_id))


@router.delete("/{store_id}/shelves/{shelf_code}", status_code=204)
def delete_shelf(
    store_id: str,
    shelf_code: str,
    with_planogram: bool = False,
    db: Session = Depends(get_db),
    _admin: CurrentUser = Depends(require_recent_auth(Permission.STORE_MANAGE)),   # удаление — важная операция
) -> None:
    shelf = _get_shelf(db, store_id, shelf_code)
    snapshots = db.query(ShelfSnapshot).filter(ShelfSnapshot.shelf_id == shelf_code).count()
    cameras = db.query(CameraORM).filter(CameraORM.shelf_id == shelf_code).count()
    slots = db.query(PlanogramSlotORM).filter(PlanogramSlotORM.shelf_id == shelf_code).count()
    reasons = shelf_delete_blockers(snapshots, cameras, slots, with_planogram)
    if reasons:
        raise _conflict(reasons)
    db.query(PlanogramSlotORM).filter(PlanogramSlotORM.shelf_id == shelf_code).delete(synchronize_session=False)
    db.delete(shelf)
    db.commit()


@router.delete("/{store_id}", status_code=204)
def delete_store(
    store_id: str,
    db: Session = Depends(get_db),
    _admin: CurrentUser = Depends(require_recent_auth(Permission.STORE_MANAGE)),   # удаление — важная операция
) -> None:
    store = db.get(Store, store_id)
    if store is None:
        raise ApiError(404, "store_not_found", "Магазин не найден")
    reasons = store_delete_blockers(
        shelves=db.query(ShelfORM).filter(ShelfORM.store_id == store_id).count(),
        cameras=db.query(CameraORM).filter(CameraORM.store_id == store_id).count(),
        snapshots=db.query(ShelfSnapshot).filter(ShelfSnapshot.store_id == store_id).count(),
        users=db.query(UserStoreAccess).filter(UserStoreAccess.store_id == store_id).count())
    if reasons:
        raise _conflict(reasons)
    db.delete(store)
    db.commit()
