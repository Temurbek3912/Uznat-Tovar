"""
Публичный вход покупателя (документ 04, §1, §3.5; шаг Р-3, docs/PLAN_REVIEW_2026-11-13.md): QR-код магазина -> страница -> фото
товара -> карточка. Без входа и без персональных данных.

  POST /api/v1/public/identify   кадр (тело запроса image/* или поле file формы) -> публичная карточка
  GET  /api/v1/public/store      название магазина и языки — для заголовка страницы

Что гарантируется:
  * кадр — только в памяти (D-10а): принимается без временных файлов (app/core/memory_upload.py), на распознавание по виду
    передаётся через хранилище кадров без сохранения на диск (app/core/frame_store.py) и удаляется сразу после распознавания;
    в истории и журналах кадра нет;
  * в ответе — только публичные поля: название, бренд, категория, цена. Нет id, оценок, штрихкодов, служебных полей;
  * ограничение частоты: по адресу устройства и общее на весь вход (ориентиры владельца В-3: ~10 распознаваний в минуту с одного
    устройства, до 20 в минуту в пик);
  * демонстрационная заглушка вместо модели (ответам верить нельзя) — покупателю честное «не узнали», а не случайный товар;
  * ошибки — понятным языком без технических подробностей (без «docker compose», адресов и т. п.).
Пароли, токены и коды здесь не передаются, поэтому вход работает и по обычному HTTP (утверждено: первая локальная версия —
<input capture> без HTTPS, документ 14, §2.1 п. 10).
"""
from __future__ import annotations

import logging
import math

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.errors import ApiError
from app.core.memory_upload import read_frame
from app.core.throttle import LoginThrottle
from app.db.models import Product, Store
from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/public", tags=["public"])

PUBLIC_IDENTIFY_MAX_MB = 10
PER_DEVICE_PER_MINUTE = 12           # чуть выше ориентира В-3 (≈10 в минуту с устройства): повторный снимок не упирается в лимит
TOTAL_PER_MINUTE = 60                # на весь вход: втрое больше пика В-3 — запас, но не бесконечность
LANGUAGES = ("ru", "uz", "uz-Cyrl", "en")

# Скользящее окно в минуту. Тот же проверенный счётчик, что у входа сотрудников: «логин» здесь — общий ключ всего входа.
_ALL = "*"
rate_limit = LoginThrottle(max_failures_per_user=TOTAL_PER_MINUTE, max_failures_per_ip=PER_DEVICE_PER_MINUTE, window_seconds=60)

UNAVAILABLE = ("Сейчас не получается узнать товар. Попробуйте ещё раз чуть позже или спросите сотрудника магазина.")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _check_rate(request: Request) -> None:
    ip = _client_ip(request)
    wait = rate_limit.check(_ALL, ip)
    if wait > 0:
        raise ApiError(429, "public_rate_limited", "Слишком много снимков подряд. Подождите немного и попробуйте снова",
                       {"retry_after": wait}, headers={"Retry-After": str(math.ceil(wait))})
    rate_limit.record_failure(_ALL, ip)


def _price(value) -> float | None:
    return round(float(value), 2) if value is not None else None


def public_card(product: Product | None) -> dict | None:
    if product is None:
        return None
    return {"name": product.name, "brand": product.brand or "", "category": product.category or "", "price": _price(product.expected_price)}


def public_result(db: Session, result: dict) -> dict:
    """Ответ распознавания -> то, что можно показать покупателю. Товары — только из базы, только публичные поля."""
    status = result.get("status")
    if result.get("demo_mode"):
        logger.warning("Публичный вход: модель распознавания не загружена (заглушка) — покупателю ответ «не узнали»")
        return {"status": "unknown", "by": None, "product": None, "candidates": []}
    if status == "empty_catalog":
        status = "unknown"
    product = db.get(Product, result["product"]["id"]) if result.get("product") else None
    candidates = []
    if status == "uncertain":
        for item in result.get("candidates") or []:
            card = public_card(db.get(Product, item.get("id")))
            if card is not None:
                candidates.append(card)
    if status not in ("matched", "uncertain") or product is None:
        return {"status": "unknown", "by": None, "product": None, "candidates": []}
    return {"status": status, "by": result.get("by"), "product": public_card(product), "candidates": candidates}


@router.post("/identify")
async def public_identify(request: Request, db: Session = Depends(get_db)) -> dict:
    """Покупатель узнаёт товар по фото (штрихкод на фото — точнее и быстрее). Ответ: status matched | uncertain | unknown,
    product — публичная карточка, candidates — варианты при uncertain."""
    _check_rate(request)
    data = await read_frame(request, PUBLIC_IDENTIFY_MAX_MB * 1024 * 1024)
    from app.api.routes_catalog import run_identify     # маршруты каталога импортируют воркер: только при первом запросе
    try:
        result = await run_in_threadpool(run_identify, db, data)
    except ApiError as exc:
        if exc.status_code >= 500:                       # очередь, воркер, хранилище кадров: покупателю — без подробностей
            logger.warning("Публичный вход: распознавание недоступно (%s)", exc.code)
            raise ApiError(503, "public_unavailable", UNAVAILABLE) from exc
        raise
    finally:
        del data
    return public_result(db, result)


@router.get("/store")
def public_store(db: Session = Depends(get_db)) -> dict:
    """Название магазина (одна запись Store — «этот магазин», документ 02, §4) и языки страницы. Адрес и прочее не отдаются."""
    store = db.query(Store).order_by(Store.name).first()
    return {"name": store.name if store is not None else None, "languages": list(LANGUAGES)}
