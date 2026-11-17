"""
Публичный вход покупателя (документ 04, §1, §3.5; шаг Р-3, docs/PLAN_REVIEW_2026-11-13.md): QR-код магазина -> страница -> фото
товара -> карточка. Без входа и без персональных данных.

  POST /api/v1/public/identify   кадр (тело запроса image/* или поле file формы) -> публичная карточка
  GET  /api/v1/public/store      название магазина, языки и какие поля карточки показывает магазин
  GET  /api/v1/public/products   список товаров магазина: поиск (q), категория (category), «показать ещё» (offset)
  GET  /api/v1/public/categories категории товаров с числом товаров — для фильтров списка
  GET  /api/v1/public/photo/{ref} миниатюра витринного фото товара из списка

Что гарантируется:
  * кадр — только в памяти (D-10а): принимается без временных файлов (app/core/memory_upload.py), на распознавание по виду
    передаётся через хранилище кадров без сохранения на диск (app/core/frame_store.py) и удаляется сразу после распознавания;
    в истории и журналах кадра нет;
  * в ответе — только публичные поля: название, бренд, категория и то, что магазин включил в карточку (цена, производитель,
    страна, вес/объём, состав, описание, наличие — app/services/shop_card.py; срока годности нет никогда). Нет оценок,
    штрихкодов, служебных полей. В списке товаров у товара есть ref — номер записи для его миниатюры (по нему можно получить
    только миниатюру товара, который покупатель и так видит в списке);
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

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.services import shop_card
from app.services.product_kinds import is_kind
from app.core.errors import ApiError
from app.core.memory_upload import read_frame
from app.core.throttle import LoginThrottle
from app.db.models import PRODUCT_ARCHIVED, Product, Store
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
    """Адрес телефона для «12 снимков в минуту с устройства». На входе покупателя из интернета (app/core/customer_gate.py) все
    запросы приходят от туннеля с 127.0.0.1 — там адрес берётся из заголовка туннеля; нет заголовка — None (действует только
    общий предел входа, а не «12 в минуту на весь магазин»)."""
    state = request.scope.get("state") or {}
    if state.get("customer_entrance"):
        return state.get("customer_ip")
    return request.client.host if request.client else None


def _check_rate(request: Request) -> None:
    ip = _client_ip(request)
    unknown = ip is None and bool((request.scope.get("state") or {}).get("customer_entrance"))
    wait = rate_limit.check(_ALL, ip, skip_ip=unknown)
    if wait > 0:
        raise ApiError(429, "public_rate_limited", "Слишком много снимков подряд. Подождите немного и попробуйте снова",
                       {"retry_after": wait}, headers={"Retry-After": str(math.ceil(wait))})
    rate_limit.record_failure(_ALL, ip, skip_ip=unknown)


BROWSE_PER_DEVICE_PER_MINUTE = 240  # список товаров: поиск по буквам и миниатюры — запросов больше, чем снимков, но они лёгкие
BROWSE_TOTAL_PER_MINUTE = 3000
browse_limit = LoginThrottle(max_failures_per_user=BROWSE_TOTAL_PER_MINUTE, max_failures_per_ip=BROWSE_PER_DEVICE_PER_MINUTE,
                             window_seconds=60)
LIST_LIMIT = 30
LIST_MAX_LIMIT = 60


def _check_browse(request: Request) -> None:
    ip = _client_ip(request)
    unknown = ip is None and bool((request.scope.get("state") or {}).get("customer_entrance"))
    wait = browse_limit.check(_ALL, ip, skip_ip=unknown)
    if wait > 0:
        raise ApiError(429, "public_browse_limited", "Слишком много запросов подряд. Подождите немного",
                       {"retry_after": wait}, headers={"Retry-After": str(math.ceil(wait))})
    browse_limit.record_failure(_ALL, ip, skip_ip=unknown)


def _this_store(db: Session) -> Store | None:
    return db.query(Store).order_by(Store.name).first()


def card_fields(db: Session) -> tuple[str, ...]:
    """Поля карточки, которые показывает этот магазин (одна запись Store — «этот магазин», документ 02, §4)."""
    return shop_card.store_fields(_this_store(db))


def public_card(product: Product | None, fields: tuple[str, ...] = shop_card.DEFAULT_FIELDS) -> dict | None:
    if product is None or product.status == PRODUCT_ARCHIVED:      # товар в архиве покупатель не видит (последний рубеж)
        return None
    return shop_card.card(product, fields)


def public_result(db: Session, result: dict) -> dict:
    """Ответ распознавания -> то, что можно показать покупателю. Товары — только из базы, только публичные поля."""
    status = result.get("status")
    if result.get("demo_mode"):
        logger.warning("Публичный вход: модель распознавания не загружена (заглушка) — покупателю ответ «не узнали»")
        return {"status": "unknown", "by": None, "product": None, "candidates": []}
    if status == "empty_catalog":
        status = "unknown"
    if status == "not_in_catalog" and isinstance(result.get("guess"), dict):
        # товара нет в базе магазина, но программа его назвала (документ 07, §10.6) или назвала хотя бы вид (§10.7):
        # только название, бренд и код вида — без источника
        guess = result["guess"]
        kind = str(guess.get("kind") or "")
        name = str(guess.get("name") or "")
        if name or is_kind(kind):
            return {"status": "not_in_catalog", "by": None, "product": None, "candidates": [],
                    "guess": {"name": name, "brand": str(guess.get("brand") or "") if name else "",
                              "kind": kind if is_kind(kind) else ""}}
        return {"status": "unknown", "by": None, "product": None, "candidates": []}
    product = db.get(Product, result["product"]["id"]) if result.get("product") else None
    if product is not None and product.status == PRODUCT_ARCHIVED:
        product = None
    fields = card_fields(db)
    candidates = []
    if status == "uncertain":
        for item in result.get("candidates") or []:
            card = public_card(db.get(Product, item.get("id")), fields)
            if card is not None:
                candidates.append(card)
    if status not in ("matched", "uncertain") or product is None:
        return {"status": "unknown", "by": None, "product": None, "candidates": []}
    return {"status": status, "by": result.get("by"), "product": public_card(product, fields), "candidates": candidates}


@router.post("/identify")
async def public_identify(request: Request, lang: str | None = None, db: Session = Depends(get_db)) -> dict:
    """Покупатель узнаёт товар по фото (штрихкод на фото — точнее и быстрее). Ответ: status matched | uncertain |
    not_in_catalog | unknown; product — публичная карточка, candidates — варианты при uncertain, guess — название товара,
    которого нет в базе (not_in_catalog). lang — язык страницы (ru, uz, uz-Cyrl, en): на нём называется товар вне базы."""
    _check_rate(request)
    data = await read_frame(request, PUBLIC_IDENTIFY_MAX_MB * 1024 * 1024)
    from app.api.routes_catalog import run_identify     # маршруты каталога импортируют воркер: только при первом запросе
    try:
        from app.services.product_namer import normalize_lang
        from app.services import model_gate                 # пока идёт распознавание, ИИ-помощник модель не трогает
        with model_gate.identify_turn():
            result = await run_in_threadpool(run_identify, db, data, normalize_lang(lang))
    except ApiError as exc:
        if exc.status_code >= 500:                       # очередь, воркер, хранилище кадров: покупателю — без подробностей
            logger.warning("Публичный вход: распознавание недоступно (%s)", exc.code)
            raise ApiError(503, "public_unavailable", UNAVAILABLE) from exc
        raise
    finally:
        del data
    answer = public_result(db, result)
    if not result.get("demo_mode"):          # заглушка вместо модели — не скан, а неготовность программы: в счёт не идёт
        from app.services import scan_log      # итог скана для помощника «почему не узнал» (без кадра и без покупателя)
        product = result.get("product") if isinstance(result.get("product"), dict) else None
        scan_log.record(db, answer, product_id=product.get("id") if answer.get("product") and product else None)
    return answer


@router.get("/store")
def public_store(db: Session = Depends(get_db)) -> dict:
    """Название магазина, языки страницы и поля карточки, которые показывает магазин. Адрес и прочее не отдаются."""
    store = _this_store(db)
    return {"name": store.name if store is not None else None, "languages": list(LANGUAGES),
            "card_fields": list(shop_card.store_fields(store))}


def _active():
    return Product.status != PRODUCT_ARCHIVED


def _media_root():
    from app.core.config import get_settings, resolve_project_path
    return resolve_project_path(get_settings().media_root)


def _folder_photo(product: Product):
    """Фото из папки data/catalog-uploads у товара без записей о фото в базе (прежний способ хранения)."""
    from app.services.catalog_upload import list_photo_files, uploads_root

    files = list_photo_files(uploads_root(_media_root()), product.sku)
    return files[0] if files else None


@router.get("/products")
def public_products(request: Request, q: str = "", category: str = "", offset: int = 0, limit: int = LIST_LIMIT,
                    db: Session = Depends(get_db)) -> dict:
    """Список товаров магазина для покупателя (только товары в каталоге, не в архиве). q — поиск по названию, бренду и
    штрихкоду (как на странице «Товары»: регистр, ё/е, узбекские апострофы); category — точное название категории.
    Ответ: total, items (карточка + ref и has_photo), next_offset (None — товаров больше нет)."""
    from app.services.product_images import PhotoRegistry
    from app.services.product_search import apply_search

    _check_browse(request)
    limit, offset = max(1, min(int(limit), LIST_MAX_LIMIT)), max(0, int(offset))
    query = db.query(Product).filter(_active())
    category = (category or "").strip()
    if category:
        query = query.filter(Product.category == category)
    query, exact = apply_search(db, query, (q or "")[:100], columns=(Product.name, Product.brand))
    total = query.count()
    order = (func.lower(Product.name), Product.sku)
    if exact is not None:
        order = (exact.desc(), *order)
    page = query.order_by(*order).offset(offset).limit(limit).all()
    fields = card_fields(db)
    covers = PhotoRegistry(db).summaries([p.id for p in page])
    items = []
    for p in page:
        item = shop_card.card(p, fields)
        summary = covers.get(p.id)
        item["ref"] = p.id
        item["has_photo"] = bool(summary.cover_id) if summary is not None else _folder_photo(p) is not None
        items.append(item)
    after = offset + len(page)
    return {"total": total, "items": items, "next_offset": after if after < total else None}


@router.get("/categories")
def public_categories(request: Request, db: Session = Depends(get_db)) -> dict:
    """Категории товаров в каталоге (без пустых) с числом товаров — по алфавиту."""
    _check_browse(request)
    rows = (db.query(Product.category, func.count(Product.id)).filter(_active())
            .filter(Product.category.isnot(None)).filter(Product.category != "")
            .group_by(Product.category).all())
    items = sorted(({"name": name, "count": int(count)} for name, count in rows if str(name).strip()),
                   key=lambda x: x["name"].lower())
    return {"items": items}


@router.get("/photo/{ref}")
def public_photo(ref: str, request: Request, db: Session = Depends(get_db)) -> Response:
    """Миниатюра витринного фото товара из списка. Товар в архиве, чужой номер, нет фото — 404 без подробностей."""
    from app.db.models import ProductImage
    from app.services.catalog_upload import cached_thumbnail
    from app.services.product_images import PhotoRegistry, photo_file

    _check_browse(request)
    product = db.get(Product, ref) if ref and len(ref) <= 64 else None
    source = None
    if product is not None and product.status != PRODUCT_ARCHIVED:
        summary = PhotoRegistry(db).summaries([product.id]).get(product.id)
        if summary is not None:
            if summary.cover_id:
                source = photo_file(_media_root(), db.get(ProductImage, summary.cover_id))
        else:
            source = _folder_photo(product)
    if source is None:
        raise ApiError(404, "public_photo_not_found", "Фото нет")
    return Response(content=cached_thumbnail(source), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=600"})
