"""
ИИ-помощник владельца (документ 16): текстовый чат по данным каталога. Этап A1 — смотрит; этап A2 — предлагает изменения.

  GET  /api/v1/assistant/status   работает ли помощник, заняты ли покупатели распознаванием     — assistant:use (только владелец)
  GET  /api/v1/assistant/scans    ?days=30 — сводка сканов покупателей числами, без модели       — + catalog:read
  POST /api/v1/assistant/chat     {messages: [{role, content}], lang} -> {reply, tools, seconds, proposal, ui}
  POST /api/v1/assistant/act      {action: предложение} — владелец нажал «Сделать» (или «Отменить»)  — + catalog:write
  POST /api/v1/assistant/act/add  форма: name, price, brand, category, barcode, files — новый товар    — + catalog:write

Сама модель ничего не меняет: /chat только ПРЕДЛАГАЕТ (proposal), а /act выполняет предложение после нажатия владельца — тем же
кодом и с теми же проверками, что страница «Товары» (app/api/routes_catalog.py), с записью в журнал действий (via="assistant")
и с обратным действием для кнопки «Отменить». Переписку хранит страница; сервер её не сохраняет (app/services/assistant.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api import routes_catalog
from app.api.deps import require_permission
from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.security import CurrentUser, Permission
from app.db.session import get_db
from app.db.models import PRODUCT_ACTIVE, Product
from app.services import assistant, assistant_actions, assistant_tools, audit, model_gate

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

_UNAVAILABLE = {
    "disabled": "Помощник выключен на этом компьютере: здесь нет модели ИИ (нужна видеокарта NVIDIA от 6 ГБ).",
    "model_unreachable": "Модель ИИ не отвечает. Возможно, она ещё загружается — попробуйте через минуту.",
    "bad_answer": "Помощник не смог ответить на этот вопрос. Попробуйте спросить по-другому.",
}


class ChatMessage(BaseModel):
    role: str = Field(max_length=16)
    content: str = Field(max_length=4000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(max_length=40)
    lang: str = Field(default="ru", max_length=16)


class ActIn(BaseModel):
    action: dict


@router.get("/status")
def assistant_status(user: CurrentUser = Depends(require_permission(Permission.ASSISTANT_USE))) -> dict:
    available = bool(assistant.model_address(get_settings()))
    can_change = any(t.kind == "propose" for t in assistant_tools.allowed_tools(user))      # право catalog:write
    return {"available": available, "mode": "change" if can_change else "read",
            "reason": None if available else "disabled", "memory": assistant.memory_limits(),
            "customers_busy": model_gate.identify_recent()}


@router.get("/scans")
def assistant_scans(
    days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db),
    _assistant: CurrentUser = Depends(require_permission(Permission.ASSISTANT_USE)),
    _reader: CurrentUser = Depends(require_permission(Permission.CATALOG_READ)),
) -> dict:
    """Сводка «Почему не узнал» сверху страницы помощника — считает программа, модель не нужна (цифры всегда верные, видны и
    там, где модели нет). Те же числа получает модель через scan_problems (документ 16, §10.5)."""
    return assistant_tools.scan_report(db, days)


@router.post("/chat")
async def assistant_chat(
    body: ChatIn,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.ASSISTANT_USE)),
) -> dict:
    messages = [m.model_dump() for m in body.messages]
    try:
        reply = await run_in_threadpool(assistant.chat, db, user, messages, body.lang, get_settings())
    except ValueError as exc:
        raise ApiError(422, "assistant_empty", str(exc)) from exc
    except assistant.AssistantBusy as exc:
        raise ApiError(503, "assistant_busy", "Помощник занят: сейчас модель узнаёт товары для покупателей. Основные функции "
                                              "работают — спросите ещё раз через несколько секунд.") from exc
    except assistant.AssistantUnavailable as exc:
        raise ApiError(503, f"assistant_{exc.reason}", _UNAVAILABLE.get(exc.reason, _UNAVAILABLE["bad_answer"])) from exc
    return {"reply": reply.text, "tools": reply.tools, "seconds": reply.seconds, "proposal": reply.proposal, "ui": reply.ui,
            "proposal_error": reply.proposal_error, "no_card": reply.no_card}


@router.post("/act")
def assistant_act(
    body: ActIn,
    db: Session = Depends(get_db),
    _assistant: CurrentUser = Depends(require_permission(Permission.ASSISTANT_USE)),
    user: CurrentUser = Depends(require_permission(Permission.CATALOG_WRITE)),
) -> dict:
    """Владелец нажал «Сделать» в карточке помощника (или «Отменить» после неё). Проверки — те же, что у страницы «Товары»;
    версия товара сверяется: если его успели изменить, 409, а не перезапись."""
    action = body.action
    if action.get("kind") == "bulk":
        return _act_bulk(db, user, action)
    kind, product_id = action.get("kind"), action.get("product_id")
    version = action.get("version") if isinstance(action.get("version"), int) else None
    if kind not in ("update", "status") or not isinstance(product_id, str):
        raise ApiError(422, "assistant_action_invalid", "Помощник предложил непонятное действие — попросите ещё раз.")
    product = db.get(Product, product_id)
    if product is None:
        raise ApiError(404, "product_not_found", "Товар не найден.")
    if kind == "status":
        before = product.status or PRODUCT_ACTIVE
        summary = routes_catalog.change_product_status(
            db, user, product_id, routes_catalog.StatusUpdate(status=str(action.get("status")), reason="ИИ-помощник",
                                                              expected_version=version), via="assistant")
        undo = {"kind": "status", "product_id": product_id, "sku": summary.sku, "name": summary.name,
                "version": summary.version, "status": before}
        return {"ok": True, "product": summary.model_dump(), "undo": undo if summary.status != before else None}
    changes = action.get("changes") if isinstance(action.get("changes"), dict) else {}
    try:
        fields = assistant_actions.to_product_update(action)
    except assistant_actions.ActionError as exc:
        raise ApiError(422, "assistant_action_invalid", str(exc)) from exc
    before = {f: assistant_actions.current_value(db, product, f) for f in changes}      # «было» — из базы, а не со страницы
    summary = routes_catalog.update_product(product_id, routes_catalog.ProductUpdate(**fields, expected_version=version), db, user)
    audit.record(db, "assistant.product.updated", actor=audit.AuditActor(user.id, "assistant"), target_type="product",
                 target_id=product_id, before=assistant_actions.audit_fields(before),
                 after=assistant_actions.audit_fields(changes))
    db.commit()
    undo = assistant_actions.undo_of_update({**action, "before": before}, summary.version, summary.name)
    return {"ok": True, "product": summary.model_dump(), "undo": undo}


_BULK_COLUMN = {"price": "expected_price", "category": "category", "brand": "brand", "in_stock": "in_stock", "status": "status"}


def _act_bulk(db: Session, user: CurrentUser, action: dict) -> dict:
    """Массовое изменение (этап A3): все строки карточки — одной транзакцией. Любая ошибка (товар успели изменить — 409, не
    найден, неверное значение) — не меняется НИЧЕГО. Значения проверяются заново (страница могла прислать что угодно), «было» —
    из базы. Запись в журнал — по каждому товару, как при изменении одного. Ответ — обратное действие «Отменить всё»."""
    field, rows = action.get("field"), action.get("rows")
    if field not in assistant_actions.BULK_FIELDS or not isinstance(rows, list) or not rows \
            or len(rows) > assistant_actions.BULK_MAX or not all(isinstance(r, dict) for r in rows):
        raise ApiError(422, "assistant_action_invalid", "Помощник предложил непонятное действие — попросите ещё раз.")
    ids = [r.get("product_id") for r in rows]
    if not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ApiError(422, "assistant_action_invalid", "Помощник предложил непонятное действие — попросите ещё раз.")
    column = _BULK_COLUMN[field]
    actor = audit.AuditActor(user.id, "assistant")
    reason = f"ИИ-помощник: массовое изменение (товаров: {len(rows)})"
    undo_rows = []
    try:
        for row in rows:
            product = db.get(Product, row["product_id"])
            if product is None:
                raise ApiError(404, "product_not_found", f"Товар «{str(row.get('name') or '')[:60]}» не найден — ничего не изменено.")
            version = row.get("version") if isinstance(row.get("version"), int) else None
            routes_catalog._check_version(product, version)        # noqa: SLF001 — та же проверка, что у страницы «Товары»
            try:
                after = assistant_actions.clean_bulk_value(field, row.get("after"))
            except assistant_actions.ActionError as exc:
                raise ApiError(422, "assistant_action_invalid", str(exc)) from exc
            before = (product.status or PRODUCT_ACTIVE) if field == "status" else getattr(product, column)
            if before == after:
                continue
            setattr(product, column, after)
            routes_catalog._bump(product)                           # noqa: SLF001
            if field == "status":
                audit.record(db, routes_catalog._STATUS_ACTIONS[after], actor=actor, target_type="product",  # noqa: SLF001
                             target_id=product.id, before={"status": before}, after={"status": after}, reason=reason)
            else:
                audit.record(db, "assistant.product.updated", actor=actor, target_type="product", target_id=product.id,
                             before={field: before}, after={field: after}, reason=reason)
            undo_rows.append({"product_id": product.id, "sku": product.sku, "name": product.name, "before": after,
                              "after": before, "_product": product})
        db.commit()
    except Exception:
        db.rollback()
        raise
    for row in undo_rows:
        row["version"] = row.pop("_product").version
    undo = {**{k: action.get(k) for k in ("field", "change", "value", "where", "query", "categories")}, "kind": "bulk", "undo": True,
            "rows": undo_rows, "skipped": 0} if undo_rows else None
    return {"ok": True, "changed": len(undo_rows), "undo": undo}


@router.post("/act/add", status_code=202)
def assistant_act_add(
    name: str = Form(""),
    price: str = Form(""),
    brand: str = Form(""),
    category: str = Form(""),
    barcode: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    _assistant: CurrentUser = Depends(require_permission(Permission.ASSISTANT_USE)),
    user: CurrentUser = Depends(require_permission(Permission.CATALOG_WRITE)),
) -> dict:
    """Новый товар из карточки помощника: владелец проверил название и цену, добавил фото и нажал «Сделать». Тот же приём, что
    у «Добавить товар» (POST /catalog/products/upload); ход — GET /catalog/products/tasks/{task_id}."""
    out = routes_catalog.upload_product(name=name, price=price, brand=brand, category=category, barcode=barcode, files=files,
                                        db=db, _user=user)
    audit.record(db, "assistant.product.added", actor=audit.AuditActor(user.id, "assistant"), target_type="product",
                 after={"sku": out.get("sku"), "name": out.get("name"), "photos": out.get("photos")})
    db.commit()
    return out
