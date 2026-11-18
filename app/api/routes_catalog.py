"""
Каталог SKU: чтение/поиск (F12 — нужен для "исправить" в очереди ревью), добавление нового товара и «Узнать товар».

Тонкий API-слой: логика приёма фото, артикулов и ответов — в app/services/catalog_upload.py и identify_service.py (проверяются
офлайн), ML живёт в процессе воркера, а не API (это два разных контейнера, см. docker-compose.yml), поэтому добавление и
распознавание — задачи Celery (app/worker.py). Чтение/поиск — обычный синхронный SQL.

  GET   /products                      список/поиск (+ число фото, цена, штрих-коды) — все роли с catalog:read
  POST  /products/upload               НОВЫЙ товар: название + фото (+ штрих-код)  — только admin
  POST  /products/{id}/photos          добавить фото (штрих-код на фото привяжется) — только admin
  PATCH /products/{id}                 название/цена/бренд/категория, ±штрих-код   — только admin
  GET   /products/{id}/photo           миниатюра или фото товара                   — все роли с catalog:read
  GET   /products/{id}/images          фото товара из базы: эталоны, витринные, выведенные — все роли с catalog:read
  PATCH /products/{id}/images/{img}    вывести из эталонов / вернуть / сделать витринным — только admin (журнал)
  GET   /products/tasks/{task_id}      ход добавления (state: working/done/failed)
  POST  /identify                      «Узнать товар»: кадр -> штрих-код или вид   — все роли с catalog:read
  POST  /products                      прежний способ: JSON со ссылками на фото    — только admin
"""
from __future__ import annotations

from pathlib import Path

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_permission
from app.core.app_errors import AppError
from app.core.config import get_settings, resolve_project_path
from app.core.errors import ApiError, from_app_error, from_catalog_error, reason
from app.core.frame_store import FrameStoreError, frame_ref, get_frame_store
from app.core.memory_upload import read_frame
from app.core.schemas import ProductIn
from app.core.security import CurrentUser, Permission
from app.db.models import PRODUCT_ACTIVE, PRODUCT_ARCHIVED, Product, ProductBarcodeORM, ProductImage
from app.db.session import get_db
from app.pipeline.imageio import load_rgb
from app.services.barcode import (
    decode_image,
    explain_invalid,
    normalize_code,
    plan_links,
)
from app.services.catalog_upload import (
    DEFAULT_BRAND,
    DEFAULT_CATEGORY,
    MAX_PHOTOS_PER_REQUEST,
    MAX_TEXT_LEN,
    CatalogAddError,
    InputError,
    NormalizedPhoto,
    PhotoError,
    clean_name,
    clean_text,
    describe_task,
    find_duplicate,
    list_photo_files,
    make_sku,
    cached_thumbnail,
    normalize_photo,
    parse_price,
    save_photos,
    uploads_root,
)
from app.services.frame_lifecycle import FRAME_TTL_SECONDS
from app.services import customer_qr, model_gate, shop_card
from app.services.product_search import apply_search
from app.services.product_images import (
    DISPLAY,
    REFERENCE,
    PhotoRegistry,
    PhotoSummary,
    in_recognition,
    photo_file,
)
from app.worker import (
    add_catalog_photos_task,
    add_catalog_product_task,
    celery_app,
    identify_product_task,
)

IDENTIFY_MAX_MB = 10               # кадр с камеры — десятки/сотни КБ; больше — явная ошибка клиента
IDENTIFY_WAIT_SECONDS = 25         # сколько API ждёт ответ воркера (занят снимком полки — кадр постоит в очереди)
IDENTIFY_KEEP_SECONDS = FRAME_TTL_SECONDS  # ТК-2: временный кадр живёт не больше 2 минут (app/services/frame_lifecycle.py)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


class ProductSummary(BaseModel):
    id: str
    sku: str
    name: str
    brand: str
    category: str
    expected_price: float | None = None
    photo_count: int = 0           # фото товара в базе (product_images, все роли); товар без записей в базе (фото ещё не
                                   # перенесены воркером) — по-прежнему число файлов в data/catalog-uploads
    barcodes: list[str] = []       # привязанные штрих-коды (нормализованные)
    reference_count: int | None = None   # фото, по которым товар узнаётся (активные эталоны); None — фото ещё не в базе
    cover_image_id: str | None = None    # фото для миниатюры (витринное, иначе первый эталон); None — нет файла в программе
    status: str = PRODUCT_ACTIVE         # active — в каталоге; archived — в архиве (не распознаётся, покупатель не видит)
    version: int = 1                     # номер правки: прислать в expected_version, чтобы не перезаписать чужую правку
    # сведения для карточки покупателя (миграция 0007; что из них видит покупатель — настройка магазина, app/services/shop_card.py)
    manufacturer: str | None = None
    country: str | None = None
    net_content: str | None = None
    ingredients: str | None = None
    description: str | None = None
    in_stock: bool | None = None


class ImageUpdate(BaseModel):
    """Что изменить у фото товара. Не присланное поле не меняется. Фото и файл не удаляются никогда."""

    active: bool | None = None     # False — вывести из эталонов (в распознавании не участвует), True — вернуть
    role: str | None = None        # "reference" — эталон для распознавания, "display" — витринное (только показ в карточке)
    reason: str | None = None      # причина — для журнала действий


class ProductUpdate(BaseModel):
    """Что исправить у товара. Не присланное поле не меняется. Артикул не меняется никогда: на него опираются планограммы и импорт."""

    name: str | None = None
    brand: str | None = None
    category: str | None = None
    expected_price: str | float | None = None     # присланная цена заменяет прежнюю; пустая строка — цену убрать
    clear_price: bool = False
    add_barcode: str | None = None                # привязать ещё один штрих-код (проверяется контрольная цифра и занятость)
    remove_barcode: str | None = None             # отвязать штрих-код (ошибочно привязанный)
    # сведения для покупателя: присланное заменяет прежнее, пустая строка — убрать; in_stock: true/false, clear_in_stock — «не указано»
    manufacturer: str | None = None
    country: str | None = None
    net_content: str | None = None
    ingredients: str | None = None
    description: str | None = None
    in_stock: bool | None = None
    clear_in_stock: bool = False
    expected_version: int | None = None           # версия, которую видел человек; товар успели изменить — 409, а не перезапись


class StatusUpdate(BaseModel):
    """Убрать товар в архив (archived) или вернуть в каталог (active). Товар не удаляется: фото, штрихкоды и история остаются."""

    status: str
    reason: str | None = None                     # причина — для журнала действий
    expected_version: int | None = None


def _media_root() -> Path:
    return resolve_project_path(get_settings().media_root)


def _photos_dir() -> Path:
    return uploads_root(_media_root())


def _summary(product: Product, barcodes: list[str] | None = None, photos: PhotoSummary | None = None) -> ProductSummary:
    """Сводка товара. Фото — из базы (product_images); если записей о фото у товара нет (перенос воркером ещё не выполнен),
    число фото — по папке data/catalog-uploads, как раньше: прежние клиенты видят то же самое."""
    if photos is not None:
        count, refs, cover = photos.total, photos.references, photos.cover_id
    else:
        count, refs, cover = len(list_photo_files(_photos_dir(), product.sku)), None, None
    return ProductSummary(
        id=product.id, sku=product.sku, name=product.name, brand=product.brand, category=product.category,
        expected_price=product.expected_price, photo_count=count, barcodes=sorted(barcodes or []),
        reference_count=refs, cover_image_id=cover, status=product.status or PRODUCT_ACTIVE, version=product.version or 1,
        **{code: getattr(product, code, None) for code in shop_card.TEXT_FIELDS}, in_stock=getattr(product, "in_stock", None),
    )


def _photo_summaries(db: Session, product_ids: list[str]) -> dict[str, PhotoSummary]:
    return PhotoRegistry(db).summaries(product_ids)


def _barcodes_of(db: Session, product_ids: list[str]) -> dict[str, list[str]]:
    """Штрих-коды сразу для нескольких товаров одним запросом: {id товара: [коды]}."""
    out: dict[str, list[str]] = {}
    if not product_ids:
        return out
    for row in db.query(ProductBarcodeORM.barcode, ProductBarcodeORM.product_id).filter(ProductBarcodeORM.product_id.in_(product_ids)).all():
        out.setdefault(row.product_id, []).append(row.barcode)
    return out


def _owners(db: Session, codes: list[str]) -> dict[str, tuple[str, str]]:
    """{код: (id товара, название)} для уже привязанных кодов из списка."""
    if not codes:
        return {}
    links = db.query(ProductBarcodeORM.barcode, ProductBarcodeORM.product_id).filter(ProductBarcodeORM.barcode.in_(codes)).all()
    if not links:
        return {}
    names = {r.id: r.name for r in db.query(Product.id, Product.name).filter(Product.id.in_([r.product_id for r in links])).all()}
    return {r.barcode: (r.product_id, names.get(r.product_id, "?")) for r in links}


def _detect_barcodes(photos: list[NormalizedPhoto], limit: int = 3) -> list[str]:
    """Штрих-коды, которые удалось прочесть на фото товара (фото упаковки со штрих-кодом). Только удобство: любой сбой чтения —
    просто «ничего не найдено», добавление товара из-за него не падает."""
    found: list[str] = []
    for photo in photos:
        try:
            codes = decode_image(load_rgb(photo.jpeg))
        except Exception:  # noqa: BLE001
            continue
        for code in codes[:1]:
            if code not in found:
                found.append(code)
        if len(found) >= limit:
            break
    return found


def _barcode_conflict(code: str, owner: tuple[str, str]) -> dict:
    return {"message": f"Штрих-код {code} уже привязан к товару «{owner[1]}».", "product_id": owner[0], "product_name": owner[1]}


def _duplicate_error(duplicate: tuple[str, str]) -> ApiError:
    detail = {"message": f"Товар «{duplicate[1]}» уже есть в каталоге.", "product_id": duplicate[0], "product_name": duplicate[1]}
    return ApiError(409, "product_duplicate", detail, {"name": duplicate[1]})


def _barcode_conflict_error(code: str, owner: tuple[str, str]) -> ApiError:
    return ApiError(409, "barcode_taken", _barcode_conflict(code, owner), {"code": code, "name": owner[1]})


def _concurrent_update_error() -> ApiError:
    return ApiError(409, "concurrent_update", {"message": "Товар только что изменил другой запрос — обновите страницу и повторите."})


def _barcode_notes(attached: list[str], conflicts: list[tuple[str, str, str]]) -> list[dict]:
    """Сообщения человеку про штрих-коды, найденные на фото: что привязано, что не удалось (код уже у другого товара)."""
    notes = [{"photo": None, "message": f"Найден штрих-код {code} — привязан к товару."} for code in attached]
    notes += [{"photo": None, "message": f"Штрих-код {code} с фото уже привязан к товару «{name}» — к этому не привязан."} for code, _pid, name in conflicts]
    return notes


def _read_limited(upload: UploadFile, limit_bytes: int, label: str, number: int | None = None) -> bytes:
    """Читает файл, но не больше limit_bytes: заведомо огромный файл не загружаем в память. number — номер фото в форме (для текста)."""
    data = upload.file.read(limit_bytes + 1)
    if len(data) > limit_bytes:
        mb = limit_bytes // (1024 * 1024)
        if number is None:
            raise ApiError(413, "frame_too_large", f"{label}: файл больше {mb} МБ.", {"mb": mb})
        raise ApiError(413, "photo_too_large", f"{label}: файл больше {mb} МБ.", {"n": number, "mb": mb})
    return data


def _normalize_uploads(files: list[UploadFile]) -> tuple[list[NormalizedPhoto], list[dict]]:
    """Все фото формы -> проверенные JPEG + предупреждения (размыто/мелко). Первое негодное фото — 422 с его номером."""
    if not files:
        raise ApiError(422, "photo_required", "Добавьте хотя бы одно фото товара.")
    if len(files) > MAX_PHOTOS_PER_REQUEST:
        raise ApiError(422, "photos_per_request_limit", f"За один раз можно добавить не больше {MAX_PHOTOS_PER_REQUEST} фото.",
                       {"max": MAX_PHOTOS_PER_REQUEST})
    limit = get_settings().max_upload_mb * 1024 * 1024
    photos: list[NormalizedPhoto] = []
    warnings: list[dict] = []
    for number, upload in enumerate(files, start=1):
        label = f"Фото {number}"
        data = _read_limited(upload, limit, label, number)
        try:
            photo = normalize_photo(data)
        except PhotoError as exc:
            raise ApiError(422, "photo_numbered", f"{label}: {exc}",
                           {"n": number, "reason": reason(exc.code, exc.params) or str(exc)}) from exc
        photos.append(photo)
        if photo.warning():
            warnings.append({"photo": number, "message": photo.warning()})
    return photos, warnings


def _enqueue(call) -> str:
    """Постановка задачи в очередь: call — функция без аргументов вида `lambda: some_task.delay(...)` (так статический тест
    tests/test_worker_static.py видит, что .delay вызывается у настоящей Celery-задачи). Очередь (Redis) недоступна — понятная
    ошибка, а не 500."""
    try:
        return call().id
    except Exception as exc:  # noqa: BLE001 — kombu/redis бросают разные исключения
        raise ApiError(503, "queue_unavailable",
                       "Очередь задач сейчас недоступна. Проверьте, что система запущена (docker compose ps), и повторите.") from exc


@router.get("/products", response_model=list[ProductSummary])
async def search_products(
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    sort: str = "sku",
    status: str = PRODUCT_ACTIVE,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_READ)),
) -> list[ProductSummary]:
    """Поиск по каталогу — для пикера товара при "corrected" в очереди ревью (F12) и для страницы «Товары».

    q="" (по умолчанию) возвращает первые limit товаров без фильтра. Поиск — «содержит» по sku/name/brand без учёта регистра
    (и кириллицы), «ё»/«е» и вариантов узбекского апострофа; % и _ — обычные символы (app/core/text_search.py). Без полнотекстового
    индекса: при каталоге в десятки-сотни SKU этого достаточно. offset — «показать ещё»; sort="new" — сначала недавно добавленные.

    Цифры в запросе ищутся и по штрихкодам: полный код в любом написании (пробелы, UPC-A, GTIN-14, лишние ведущие нули —
    barcode.search_keys) и часть кода от 4 цифр («содержит»). Товары с точным совпадением штрихкода — первыми.

    status: active (по умолчанию — товары в каталоге), archived (архив), all (все).
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    query = db.query(Product)
    if status in (PRODUCT_ACTIVE, PRODUCT_ARCHIVED):
        query = query.filter(Product.status == status)
    elif status != "all":
        raise ApiError(422, "product_status_invalid", "Состояние товара: active (в каталоге), archived (в архиве) или all.")
    query, exact = apply_search(db, query, q)
    order = (Product.created_at.desc(), Product.sku) if sort == "new" else (Product.sku,)
    if exact is not None:                             # точное совпадение штрихкода — первым
        order = (exact.desc(), *order)
    products = query.order_by(*order).offset(offset).limit(limit).all()
    ids = [p.id for p in products]
    codes, photos = _barcodes_of(db, ids), _photo_summaries(db, ids)
    return [_summary(p, codes.get(p.id), photos.get(p.id)) for p in products]


@router.post("/products", status_code=202)
async def add_product(
    product_in: ProductIn,
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_WRITE)),  # F13: только admin
) -> dict:
    """
    Прежний способ (для скриптов): ставит в очередь добавление SKU по ССЫЛКАМ на эталонные фото. Человеку удобнее
    POST /products/upload (название + файлы). Ответ 202 — задача принята; прогресс — GET /products/tasks/{task_id}.
    """
    task = add_catalog_product_task.delay(product_in.model_dump())
    return {"task_id": task.id, "status": "queued"}


@router.post("/products/upload", status_code=202)
def upload_product(
    name: str = Form(""),
    price: str = Form(""),
    brand: str = Form(""),
    category: str = Form(""),
    barcode: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_WRITE)),
) -> dict:
    """«Добавить товар» из браузера: название + фото (+ штрих-код: введённый или найденный на фото). Артикул строится из названия
    сам. Проверки идут от дешёвых к дорогим: текст -> повтор названия/штрих-кода -> фото. Ответ 202; ход выполнения —
    GET /products/tasks/{task_id}."""
    try:
        clean = clean_name(name)
        price_value = parse_price(price)
        brand_value = clean_text(brand, label="бренд", max_len=MAX_TEXT_LEN, default=DEFAULT_BRAND)
        category_value = clean_text(category, label="категорию", max_len=MAX_TEXT_LEN, default=DEFAULT_CATEGORY)
        manual_code = None
        if barcode.strip():
            manual_code = normalize_code(barcode)
            if manual_code is None:
                raise InputError(explain_invalid(barcode))
    except InputError as exc:
        raise from_catalog_error(422, exc) from exc

    rows = db.query(Product.id, Product.name, Product.sku).all()
    duplicate = find_duplicate(clean, [(r.id, r.name) for r in rows])
    if duplicate:
        raise _duplicate_error(duplicate)

    if manual_code:
        owner = _owners(db, [manual_code]).get(manual_code)
        if owner:
            raise _barcode_conflict_error(manual_code, owner)

    photos, warnings = _normalize_uploads(files)
    barcodes: list[str] = []
    if manual_code:
        barcodes = [manual_code]
    else:
        detected = _detect_barcodes(photos)
        plan = plan_links(detected, _owners(db, detected), None)
        barcodes = plan.to_add
        warnings += _barcode_notes(plan.to_add, plan.conflicts)
    try:
        sku = make_sku(clean, [r.sku for r in rows])
        saved = save_photos(_photos_dir(), sku, photos)
    except CatalogAddError as exc:
        raise from_catalog_error(422, exc) from exc

    product_in = ProductIn(sku=sku, name=clean, brand=brand_value, category=category_value, expected_price=price_value,
                           reference_image_urls=saved.refs, barcodes=barcodes)
    task_id = _enqueue(lambda: add_catalog_product_task.delay(product_in.model_dump()))
    return {"task_id": task_id, "status": "queued", "sku": sku, "name": clean, "photos": len(photos), "warnings": warnings,
            "barcodes": barcodes}


@router.post("/products/{product_id}/photos", status_code=202)
def add_product_photos(
    product_id: str,
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_WRITE)),
) -> dict:
    """Больше ракурсов — увереннее узнаёт: добавляет фото к УЖЕ существующему товару (запись в БД не меняется)."""
    product = db.get(Product, product_id)
    if product is None:
        raise ApiError(404, "product_not_found", "Товар не найден.")
    photos, warnings = _normalize_uploads(files)
    try:
        saved = save_photos(_photos_dir(), product.sku, photos)
    except CatalogAddError as exc:
        raise from_catalog_error(422, exc) from exc
    detected = _detect_barcodes(photos)
    plan = plan_links(detected, _owners(db, detected), product.id)
    added: list[str] = []
    for code in plan.to_add:
        db.add(ProductBarcodeORM(barcode=code, product_id=product.id))
        try:
            db.commit()
            added.append(code)
        except Exception:  # noqa: BLE001 — другой запрос успел занять код: это не ошибка фото
            db.rollback()
            warnings.append({"photo": None, "message": f"Штрих-код {code} только что привязан к другому товару — к этому не привязан."})
    warnings += _barcode_notes(added, plan.conflicts)
    task_id = _enqueue(lambda: add_catalog_photos_task.delay(product.id, saved.refs))
    return {"task_id": task_id, "status": "queued", "product_id": product.id, "name": product.name,
            "photos": len(photos), "warnings": warnings, "barcodes": added}


@router.patch("/products/{product_id}", response_model=ProductSummary)
def update_product(
    product_id: str,
    body: ProductUpdate,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_WRITE)),
) -> ProductSummary:
    """Исправить опечатку в названии, цену, бренд, категорию. На распознавание не влияет (оно идёт по фото), артикул не меняется."""
    product = db.get(Product, product_id)
    if product is None:
        raise ApiError(404, "product_not_found", "Товар не найден.")
    _check_version(product, body.expected_version)
    # Сначала проверяем ВСЁ и только потом применяем: ошибка в цене не должна оставить применённым новое название.
    changes: dict[str, object] = {}
    try:
        if body.name is not None:
            new_name = clean_name(body.name)
            others = db.query(Product.id, Product.name).filter(Product.id != product.id).all()
            duplicate = find_duplicate(new_name, [(r.id, r.name) for r in others])
            if duplicate:
                raise _duplicate_error(duplicate)
            changes["name"] = new_name
        if body.brand is not None:
            changes["brand"] = clean_text(body.brand, label="бренд", max_len=MAX_TEXT_LEN, default=DEFAULT_BRAND)
        if body.category is not None:
            changes["category"] = clean_text(body.category, label="категорию", max_len=MAX_TEXT_LEN, default=DEFAULT_CATEGORY)
        if body.clear_price:
            changes["expected_price"] = None
        elif body.expected_price is not None:
            changes["expected_price"] = parse_price(body.expected_price)
        for code in shop_card.TEXT_FIELDS:
            value = getattr(body, code)
            if value is not None:
                changes[code] = shop_card.clean_detail(code, value)
        if body.clear_in_stock:
            changes["in_stock"] = None
        elif body.in_stock is not None:
            changes["in_stock"] = bool(body.in_stock)
        add_code = remove_code = link = None
        if body.add_barcode is not None and body.add_barcode.strip():
            add_code = normalize_code(body.add_barcode)
            if add_code is None:
                raise InputError(explain_invalid(body.add_barcode))
            owner = _owners(db, [add_code]).get(add_code)
            if owner and owner[0] != product.id:
                raise _barcode_conflict_error(add_code, owner)
        if body.remove_barcode is not None and body.remove_barcode.strip():
            remove_code = normalize_code(body.remove_barcode)
            link = db.get(ProductBarcodeORM, remove_code) if remove_code else None
            if link is None or link.product_id != product.id:
                raise ApiError(404, "barcode_not_on_product", "Такого штрих-кода у этого товара нет.")
    except InputError as exc:
        raise from_catalog_error(422, exc) from exc
    except shop_card.CardFieldsError as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    adding = bool(add_code) and db.get(ProductBarcodeORM, add_code) is None
    _apply_product_changes(db, product, changes, link, add_code if adding else None)
    try:
        db.commit()
    except IntegrityError:
        # Уникальность штрих-кода держит база (product_barcodes.barcode — первичный ключ). Между проверкой выше и commit
        # параллельный запрос мог занять тот же код: раньше это была ошибка сервера 500. Запрос атомарен — откатываем всё.
        db.rollback()
        owner = _owners(db, [add_code]).get(add_code) if adding else None
        if owner is None or owner[0] != product.id:
            if owner is not None:
                raise _barcode_conflict_error(add_code, owner) from None
            raise _concurrent_update_error() from None
        # двойное нажатие: этот же код только что привязал к ЭТОМУ товару параллельный запрос — применяем остальное без него
        product = db.get(Product, product_id)
        link = db.get(ProductBarcodeORM, remove_code) if remove_code else None
        if product is None or (remove_code and (link is None or link.product_id != product.id)):
            raise _concurrent_update_error() from None
        _apply_product_changes(db, product, changes, link, None)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise _concurrent_update_error() from None
    db.refresh(product)
    return _summary(product, _barcodes_of(db, [product.id]).get(product.id), _photo_summaries(db, [product.id]).get(product.id))


def _apply_product_changes(db: Session, product: Product, changes: dict, remove_link, add_code: str | None) -> None:
    changed = False
    for field, value in changes.items():
        changed = changed or getattr(product, field) != value
        setattr(product, field, value)
    if remove_link is not None:
        db.delete(remove_link)
        changed = True
    if add_code:
        db.add(ProductBarcodeORM(barcode=add_code, product_id=product.id))
        changed = True
    if changed:
        _bump(product)


def _bump(product: Product) -> None:
    product.version = (product.version or 1) + 1
    product.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def _check_version(product: Product, expected: int | None) -> None:
    """Человек правил товар, который успели изменить (другой сотрудник, другая вкладка): 409 вместо тихой перезаписи."""
    if expected is not None and expected != (product.version or 1):
        raise _concurrent_update_error()


_STATUS_ACTIONS = {PRODUCT_ARCHIVED: "catalog.product.archived", PRODUCT_ACTIVE: "catalog.product.restored"}


@router.post("/products/{product_id}/status", response_model=ProductSummary)
def set_product_status(
    product_id: str,
    body: StatusUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CATALOG_WRITE)),
) -> ProductSummary:
    """Убрать товар в архив или вернуть. В архиве товар не распознаётся (ни по виду, ни по штрихкоду), покупатель его не видит,
    в списке «Товары» он только в разделе «Архив». Ничего не удаляется: вернули — всё работает, как раньше (индекс не
    пересобирается, действует со следующего кадра). Запись в журнале действий — в той же транзакции."""
    return change_product_status(db, user, product_id, body)


def change_product_status(db: Session, user: CurrentUser, product_id: str, body: StatusUpdate, via: str = "api") -> ProductSummary:
    """То же для ИИ-помощника (via="assistant", app/api/routes_assistant.py): одни проверки, одна запись в журнале."""
    if body.status not in _STATUS_ACTIONS:
        raise ApiError(422, "product_status_invalid", "Состояние товара: active (в каталоге), archived (в архиве) или all.")
    product = db.get(Product, product_id)
    if product is None:
        raise ApiError(404, "product_not_found", "Товар не найден.")
    _check_version(product, body.expected_version)
    before = product.status or PRODUCT_ACTIVE
    if before != body.status:
        from app.services import audit            # здесь, а не в начале файла: журнал тянет SQLAlchemy-запросы
        product.status = body.status
        _bump(product)
        audit.record(db, _STATUS_ACTIONS[body.status], actor=audit.AuditActor(user.id, via), target_type="product",
                     target_id=product.id, before={"status": before}, after={"status": body.status},
                     reason=(body.reason or "").strip() or None)
        db.commit()
        db.refresh(product)
    return _summary(product, _barcodes_of(db, [product.id]).get(product.id), _photo_summaries(db, [product.id]).get(product.id))


@router.get("/products/tasks/{task_id}")
async def get_add_product_status(
    task_id: str,
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_WRITE)),
) -> dict:
    """Ход добавления товара/фото. Прежние поля status/result сохранены; для интерфейса — state (working/done/failed),
    message (понятным языком) и detail (техническая причина «для специалиста»)."""
    result = celery_app.AsyncResult(task_id)

    described = describe_task(result.status, result.result if result.ready() else None)
    payload = None
    if result.ready():
        # Ошибка задачи: прежнее поле result — только безопасный текст (наш или общий), не текст произвольного исключения.
        payload = described["message"] if isinstance(result.result, Exception) else result.result
    return {"task_id": task_id, "status": result.status, "result": payload, **described}


@router.get("/products/{product_id}/photo")
def product_photo(
    product_id: str,
    size: str = "thumb",
    n: int | None = None,
    image_id: str | None = None,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_READ)),
) -> Response:
    """Фото товара (size=thumb — миниатюра 240 px для списка, size=full — как сохранено). Нужен заголовок Authorization,
    поэтому страница получает его через fetch -> blob, а не через <img src>.

    Какое фото: image_id — конкретное фото из базы (GET …/images); без image_id и n — витринное фото товара из базы
    (product_images.cover_image), а если записей о фото нет — первое фото из папки, как раньше; n — прежний способ:
    n-е фото в папке data/catalog-uploads."""
    product = db.get(Product, product_id)
    if product is None:
        raise ApiError(404, "product_has_no_photos", "У товара нет фото.")
    source = None
    if image_id is not None:
        image = db.get(ProductImage, image_id)
        source = photo_file(_media_root(), image) if image is not None and image.product_id == product.id else None
    else:
        summary = _photo_summaries(db, [product.id]).get(product.id) if n is None else None
        if summary is not None:
            source = photo_file(_media_root(), db.get(ProductImage, summary.cover_id)) if summary.cover_id else None
        else:
            files = list_photo_files(_photos_dir(), product.sku)
            index = n or 0
            source = files[index] if 0 <= index < len(files) else None
    if source is None:
        raise ApiError(404, "product_has_no_photos", "У товара нет фото.")
    if size == "full":
        content = source.read_bytes()
    else:
        content = cached_thumbnail(source)
    return Response(content=content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


def _image_out(image: ProductImage, cover_id: str | None) -> dict:
    """Фото для страницы «Товары»: без путей на сервере и без содержимого (оно — через GET …/photo?image_id=)."""
    return {"id": image.id, "role": image.role, "active": bool(image.is_active), "in_recognition": in_recognition(image),
            "has_file": bool(image.storage_path), "is_cover": image.id == cover_id, "original_name": image.original_name,
            "credit": image.credit,
            "width": image.width, "height": image.height, "created_at": image.created_at, "deactivated_at": image.deactivated_at}


def _images_payload(db: Session, product: Product) -> dict:
    registry = PhotoRegistry(db)
    images = registry.images_of(product.id)
    summary = registry.summaries([product.id]).get(product.id)
    cover = summary.cover_id if summary else None
    return {"product_id": product.id, "name": product.name, "photo_count": len(images),
            "reference_count": summary.references if summary else 0, "cover_image_id": cover,
            "images": [_image_out(i, cover) for i in images]}


def _customer_address(request: Request) -> customer_qr.CustomerAddress:
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    return customer_qr.customer_address(public_url=os.environ.get(customer_qr.ENV_PUBLIC_URL), lan_ip=customer_qr.lan_ip(),
                                        port=port, scheme=request.url.scheme)


@router.get("/customer-qr")
def customer_qr_info(
    request: Request,
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_READ)),
) -> dict:
    """Какой адрес сейчас в QR для покупателей (вход из интернета или Wi-Fi магазина) и сам QR — для любого сотрудника."""
    address = _customer_address(request)
    return {"url": address.url, "source": address.source, "qr": customer_qr.qr_svg_data_uri(address.url)}


@router.get("/customer-qr/print.png")
def customer_qr_print(
    request: Request,
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_READ)),
) -> Response:
    """Лист A4 с крупным QR и надписями для печати (картинка PNG, строится в памяти)."""
    address = _customer_address(request)
    if not address.url:
        raise ApiError(409, "customer_url_unknown", "Адрес страницы покупателя не определён: компьютер не подключён к сети.")
    return Response(customer_qr.print_sheet_png(address.url), media_type="image/png",
                    headers={"Content-Disposition": 'attachment; filename="qr-pokupatel.png"', "Cache-Control": "no-store"})


@router.get("/products/{product_id}/images")
def product_images(
    product_id: str,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_READ)),
) -> dict:
    """Фото товара из базы, старые первыми: роль (эталон/витринное), выведено ли из эталонов, участвует ли в распознавании,
    витринное ли для миниатюры. Фото, которые воркер ещё не перенёс в базу, здесь не видны (они появятся после его запуска)."""
    product = db.get(Product, product_id)
    if product is None:
        raise ApiError(404, "product_not_found", "Товар не найден.")
    return _images_payload(db, product)


_PHOTO_ACTIONS = {("active", False): "catalog.photo.deactivated", ("active", True): "catalog.photo.activated",
                  ("role", DISPLAY): "catalog.photo.made_display", ("role", REFERENCE): "catalog.photo.made_reference"}


@router.patch("/products/{product_id}/images/{image_id}")
def update_product_image(
    product_id: str,
    image_id: str,
    body: ImageUpdate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_permission(Permission.CATALOG_WRITE)),
) -> dict:
    """Вывести фото из эталонов (active=false) или вернуть (true); сделать витринным (role=display) или эталоном (reference).
    Фото и файл не удаляются. Распознавание учитывает изменение со следующего кадра: воркер пропускает векторы фото, которые
    не являются активными эталонами (индекс пересобирать не нужно). Каждое изменение — запись в журнале действий, в той же
    транзакции."""
    product = db.get(Product, product_id)
    if product is None:
        raise ApiError(404, "product_not_found", "Товар не найден.")
    if body.role is not None and body.role not in (REFERENCE, DISPLAY):
        raise ApiError(422, "photo_role_invalid", "Роль фото: эталон (reference) или витринное (display).")
    try:
        _row, before, after = PhotoRegistry(db).change(image_id, active=body.active, role=body.role, product_id=product.id)
    except LookupError:
        raise ApiError(404, "photo_not_found", "Такого фото у этого товара нет.") from None
    from app.services import audit            # здесь, а не в начале файла: журнал тянет SQLAlchemy-запросы (тест логики без неё)
    actor = audit.AuditActor(user.id, "api")
    reason_text = (body.reason or "").strip() or None
    for field_name in ("active", "role"):
        if field_name in after:
            audit.record(db, _PHOTO_ACTIONS[(field_name, after[field_name])], actor=actor, target_type="product",
                         target_id=product.id, before={"image_id": image_id, field_name: before[field_name]},
                         after={"image_id": image_id, field_name: after[field_name]}, reason=reason_text)
    db.commit()
    payload = _images_payload(db, product)
    payload["changed"] = sorted(after)
    return payload


def run_identify(db: Session, data: bytes, lang: str = "ru") -> dict:
    """Общий путь «Узнать товар» для сотрудника и покупателя (D-10а: кадр не пишется на диск).

    Сначала штрих-код на кадре: известный код даёт ответ сразу (by="barcode", без модели и воркера). Иначе — распознавание по виду
    через воркер: кадр кладётся в хранилище кадров в памяти (app/core/frame_store.py: отдельный Redis без сохранения или память
    процесса), в очередь уходит только номер кадра; воркер забирает кадр и сразу удаляет. Не дождались — кадр удаляется здесь."""
    try:
        photo = normalize_photo(data, check_quality=False)
    except PhotoError as exc:
        raise from_catalog_error(422, exc) from exc
    del data

    try:
        codes = decode_image(load_rgb(photo.jpeg))
    except Exception:  # noqa: BLE001 — чтение штрих-кода не должно ломать распознавание по виду
        codes = []
    owners = _owners(db, codes)
    for code in codes:
        owner = owners.get(code)
        product = db.get(Product, owner[0]) if owner else None
        if product is not None and product.status != PRODUCT_ARCHIVED:      # товар в архиве — как будто его нет в базе
            info = {"id": product.id, "name": product.name, "brand": product.brand, "expected_price": product.expected_price, "score": 1.0}
            return {"status": "matched", "by": "barcode", "product": info, "score": 1.0, "candidates": [info], "demo_mode": False,
                    "barcode": {"code": code, "known": True}}

    jpeg = photo.jpeg            # только в памяти этого запроса: нужен, чтобы назвать товар вне базы (документ 07, §10.6)
    store = get_frame_store()
    try:
        frame_id = store.put(jpeg)
    except FrameStoreError as exc:
        logger.warning("Хранилище кадров: %s", exc)
        raise ApiError(503, "frames_unavailable",
                       "Распознавание по виду сейчас недоступно. Попробуйте снять штрихкод или повторите позже.") from exc
    del photo
    try:
        try:
            # expires: если воркер не взял задание за время ожидания (остановлен, занят), оно не выполнится позже — клиент уже ушёл.
            task = identify_product_task.apply_async(args=[frame_ref(frame_id)], expires=IDENTIFY_WAIT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(503, "queue_unavailable",
                           "Очередь задач сейчас недоступна. Проверьте, что система запущена (docker compose ps), и повторите.") from exc
        try:
            # disable_sync_subtasks=False: в облегчённом режиме задачи выполняются в процессе API по одной, и пока один снимок
            # распознаётся в своём потоке, Celery считает «идёт задача» для всего процесса — get() второго снимка без этого
            # флага падал (RuntimeError «Never call result.get() within a task») и покупатель видел «недоступно». Здесь get()
            # вызывается из запроса, а не из задачи, поэтому проверка Celery не нужна (замер 2026-11-16: было 1 из 20, стало 200 из 200).
            result = task.get(timeout=IDENTIFY_WAIT_SECONDS, propagate=False, disable_sync_subtasks=False)
        except Exception as exc:  # noqa: BLE001 — celery.exceptions.TimeoutError и ошибки бэкенда
            raise ApiError(504, "identify_timeout",
                           "Система не успела ответить. Проверьте, что она запущена и не занята (docker compose ps), и повторите.") from exc
    finally:
        store.discard(frame_id)          # воркер уже удалил кадр, если забрал; если нет — удаляем сейчас, а не через 2 минуты
    if isinstance(result, Exception):
        if isinstance(result, AppError) or type(result).__name__ in {"PhotoError", "CatalogAddError", "NotReadyError"}:
            raise from_app_error(result, fallback_code="catalog_error")        # статус по виду: 422, а «не готово» — 503
        raise ApiError(500, "recognize_failed", "Не получилось распознать товар. Попробуйте ещё раз.")
    result["by"] = "look"
    result["barcode"] = {"code": codes[0], "known": False} if codes else None
    try:
        return add_outside_name(result, jpeg, codes, lang)
    finally:
        del jpeg


def add_outside_name(result: dict, jpeg: bytes | None, codes: list[str], lang: str) -> dict:
    """Товар не найден в базе — попробовать назвать его (штрихкод -> открытые базы, иначе локальная модель «зрение + текст»;
    документ 07, §10.6). Назвали — status not_in_catalog и guess {name, brand, source}; нет — как было («не узнали»).

    «Похоже» (uncertain) тоже проверяется: поиск по виду путает похожие упаковки разных товаров (банка Red Bull «похожа» на банку
    Coca-Cola). Если название назвало ДРУГОЙ товар — ни одного общего слова с кандидатами — а модель уверена (high), это
    not_in_catalog; иначе ответ «похоже» остаётся как был (выбор за человеком).
    «Найдено» по виду (matched, by="look") тоже сверяется с надписью на этикетке (read_label: только модель, коротко), потому что
    похожие бутылки разных марок близки по виду (вода Santal «нашлась» как Montella, 0,90). Только если второй товар близко по
    счёту (LABEL_CHECK_MAX_GAP). Модель уверена и название:
      - не похоже ни на один кандидат -> not_in_catalog с догадкой;
      - похоже на другой кандидат, а не на первый -> uncertain с ним первым (общее слово может быть общим — «вода», «zero»,
        поэтому не «найдено», а «похоже»: выбор за человеком);
      - похоже на первый, модель не уверена, модели нет — ответ как был.
    При заглушке вместо модели (demo_mode) поиск по каталогу недостоверен: «нет в базе» сказать нельзя — ничего не добавляем."""
    status = result.get("status")
    if result.get("demo_mode"):
        return result
    if status in ("matched", "uncertain") and result.get("by") == "look":
        sized = _check_size(result, jpeg, lang)
        if sized is not None:
            return sized
    if status == "matched" and result.get("by") == "look":
        return _check_label(result, jpeg, lang)
    if status not in ("unknown", "empty_catalog", "uncertain"):
        return result
    from app.services.product_namer import name_product, names_overlap     # noqa: PLC0415
    guess = name_product(jpeg, codes, lang, get_settings())
    if guess is None:
        return result
    if status == "uncertain":
        if not guess.sure:
            return result
        candidates = [result.get("product") or {}] + list(result.get("candidates") or [])
        if any(names_overlap(guess, c) for c in candidates if c):
            return result
    return {**result, "status": "not_in_catalog", "guess": guess.as_dict(), "product": None, "candidates": []}


# Сверка надписи у «найдено» — только в «тесных» случаях: второй товар близко по счёту. Замер 2026-11-16 (150 снимков, как с
# телефона): у всех верных ответов, которые сверка ломала (модель на тёмном снимке уверенно «читала» другую марку), отрыв был
# ≥ 0,12; у воды Santal, «найденной» как Montella, — 0,01. При 0,08 потерь нет, а модель спрашивается на ~15 % кадров.
LABEL_CHECK_MAX_GAP = 0.08


def _gap(result: dict) -> float:
    first = result.get("product") or {}
    rivals = [c.get("score") or 0.0 for c in (result.get("candidates") or []) if c and c.get("id") != first.get("id")]
    return float(first.get("score", result.get("score")) or 0.0) - max(rivals) if rivals else 1.0


# Размеры одного товара (Pepsi 0,5 л и 1,5 л) по виду почти не различаются: на снимке вблизи масштаба нет. Другой размер,
# который по счёту ближе этого запаса к первому, считается «тем же товаром, размер не ясен» (замер — docs/CATALOG_SOURCES.md §5).
SIZE_CHECK_MAX_GAP = 0.15


def _check_size(result: dict, jpeg: bytes | None, lang: str) -> dict | None:
    """Найдено по виду, а среди близких кандидатов — другой размер того же товара (app/services/product_size.py):
      - размер прочитан на этикетке (модель уверена) -> этот размер первым, статус как был; size = "label";
      - не прочитан -> «похоже» со всеми размерами первыми: выбирает человек; size = "choose".
    Других размеров рядом нет -> None (обычная проверка)."""
    from app.services.product_size import same_size, size_of, size_variants     # noqa: PLC0415
    first = result.get("product") or {}
    others = list(result.get("candidates") or [])
    top = float(first.get("score", result.get("score")) or 0.0)
    variants = [c for c in size_variants(first, others) if float(c.get("score") or 0.0) >= top - SIZE_CHECK_MAX_GAP]
    if not variants:
        return None
    sizes = [first, *variants]
    rest = [c for c in others if c and c.get("id") not in {s.get("id") for s in sizes}]
    from app.services.product_namer import names_overlap, read_label     # noqa: PLC0415
    guess = read_label(jpeg, lang, get_settings())
    if guess is not None and guess.name and not names_overlap(guess, first):
        return None                       # на этикетке другая марка — это решает обычная сверка надписи (_check_label)
    read = size_of(guess.name) if guess is not None else None
    chosen = next((c for c in sizes if read is not None and same_size(size_of(c.get("name", "")), read)), None)
    if chosen is not None:
        ordered = [chosen, *[c for c in sizes if c.get("id") != chosen.get("id")], *rest]
        return {**result, "product": chosen, "score": chosen.get("score", result.get("score")), "candidates": ordered,
                "size": "label"}
    return {**result, "status": "uncertain", "product": first, "candidates": [*sizes, *rest], "size": "choose"}


def _check_label(result: dict, jpeg: bytes | None, lang: str) -> dict:
    """«Найдено» по виду -> сверка с надписью (см. add_outside_name)."""
    if _gap(result) >= LABEL_CHECK_MAX_GAP:
        return result
    from app.services.product_namer import agreed_kind, brand_overlap, names_overlap, read_label     # noqa: PLC0415
    settings = get_settings()
    guess = read_label(jpeg, lang, settings)
    if guess is None:
        return result
    first = result.get("product") or {}
    if names_overlap(guess, first):
        return result
    others = [c for c in (result.get("candidates") or []) if c and c.get("id") != first.get("id")]
    if any(names_overlap(guess, c) and not brand_overlap(guess, c) for c in others):
        return result                     # общее слово с другим кандидатом («сок», «вода»), но не марка — не спорим
    other = next((c for c in others if brand_overlap(guess, c)), None)
    if other is not None:
        rest = [c for c in (result.get("candidates") or []) if c and c.get("id") != other.get("id")]
        return {**result, "status": "uncertain", "product": other, "score": other.get("score", result.get("score")),
                "candidates": [other, *rest]}
    guess = agreed_kind(guess, jpeg, settings) if jpeg else guess
    return {**result, "status": "not_in_catalog", "guess": guess.as_dict(), "product": None, "candidates": []}


@router.post("/identify")
async def identify_product(
    request: Request,
    lang: str | None = None,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_READ)),
) -> dict:
    """«Узнать товар» для сотрудника: один кадр (камера или файл) -> название товара из каталога. Кадр — полем file формы
    (как раньше) или самим телом запроса (image/*); принимается целиком в памяти (app/core/memory_upload.py, D-10а).
    Ответ: см. app/services/identify_service.py + demo_mode + by + barcode."""
    data = await read_frame(request, IDENTIFY_MAX_MB * 1024 * 1024)
    from app.services.product_namer import normalize_lang     # noqa: PLC0415
    with model_gate.identify_turn():                          # пока идёт распознавание, ИИ-помощник модель не трогает
        return await run_in_threadpool(run_identify, db, data, normalize_lang(lang))
