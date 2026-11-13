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
  GET   /products/tasks/{task_id}      ход добавления (state: working/done/failed)
  POST  /identify                      «Узнать товар»: кадр -> штрих-код или вид   — все роли с catalog:read
  POST  /products                      прежний способ: JSON со ссылками на фото    — только admin
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_permission
from app.core import text_search
from app.core.app_errors import AppError
from app.core.config import get_settings, resolve_project_path
from app.core.errors import ApiError, from_app_error, from_catalog_error, reason
from app.core.schemas import ProductIn
from app.core.security import CurrentUser, Permission
from app.db.models import Product, ProductBarcodeORM
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
    identify_root,
    list_photo_files,
    make_sku,
    make_thumbnail,
    normalize_photo,
    parse_price,
    save_identify_frame,
    save_photos,
    sweep_old_files,
    uploads_root,
)
from app.services.frame_lifecycle import FRAME_TTL_SECONDS
from app.worker import (
    add_catalog_photos_task,
    add_catalog_product_task,
    celery_app,
    identify_product_task,
)

IDENTIFY_MAX_MB = 10               # кадр с камеры — десятки/сотни КБ; больше — явная ошибка клиента
IDENTIFY_WAIT_SECONDS = 25         # сколько API ждёт ответ воркера (занят снимком полки — кадр постоит в очереди)
IDENTIFY_KEEP_SECONDS = FRAME_TTL_SECONDS  # ТК-2: временный кадр живёт не больше 2 минут (app/services/frame_lifecycle.py)

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


class ProductSummary(BaseModel):
    id: str
    sku: str
    name: str
    brand: str
    category: str
    expected_price: float | None = None
    photo_count: int = 0           # фото, загруженные из браузера; у товаров из импорта папкой — 0 (там фото в catalog/)
    barcodes: list[str] = []       # привязанные штрих-коды (нормализованные)


class ProductUpdate(BaseModel):
    """Что исправить у товара. Не присланное поле не меняется. Артикул не меняется никогда: на него опираются планограммы и импорт."""

    name: str | None = None
    brand: str | None = None
    category: str | None = None
    expected_price: str | float | None = None     # присланная цена заменяет прежнюю; пустая строка — цену убрать
    clear_price: bool = False
    add_barcode: str | None = None                # привязать ещё один штрих-код (проверяется контрольная цифра и занятость)
    remove_barcode: str | None = None             # отвязать штрих-код (ошибочно привязанный)


def _media_root() -> Path:
    return resolve_project_path(get_settings().media_root)


def _photos_dir() -> Path:
    return uploads_root(_media_root())


def _summary(product: Product, barcodes: list[str] | None = None) -> ProductSummary:
    return ProductSummary(
        id=product.id, sku=product.sku, name=product.name, brand=product.brand, category=product.category,
        expected_price=product.expected_price, photo_count=len(list_photo_files(_photos_dir(), product.sku)),
        barcodes=sorted(barcodes or []),
    )


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
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_READ)),
) -> list[ProductSummary]:
    """Поиск по каталогу — для пикера товара при "corrected" в очереди ревью (F12) и для страницы «Товары».

    q="" (по умолчанию) возвращает первые limit товаров без фильтра. Поиск — «содержит» по sku/name/brand без учёта регистра
    (и кириллицы), «ё»/«е» и вариантов узбекского апострофа; % и _ — обычные символы (app/core/text_search.py). Без полнотекстового
    индекса: при каталоге в десятки-сотни SKU этого достаточно. offset — «показать ещё»; sort="new" — сначала недавно добавленные.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    query = db.query(Product)
    pattern = text_search.like_pattern(q)
    if pattern:
        query = query.filter(or_(*(text_search.sql_normalized(col, func).like(pattern, escape=text_search.ESCAPE)
                                   for col in (Product.sku, Product.name, Product.brand))))
    order = (Product.created_at.desc(), Product.sku) if sort == "new" else (Product.sku,)
    products = query.order_by(*order).offset(offset).limit(limit).all()
    codes = _barcodes_of(db, [p.id for p in products])
    return [_summary(p, codes.get(p.id)) for p in products]


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
    return _summary(product, _barcodes_of(db, [product.id]).get(product.id))


def _apply_product_changes(db: Session, product: Product, changes: dict, remove_link, add_code: str | None) -> None:
    for field, value in changes.items():
        setattr(product, field, value)
    if remove_link is not None:
        db.delete(remove_link)
    if add_code:
        db.add(ProductBarcodeORM(barcode=add_code, product_id=product.id))


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
    n: int = 0,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_READ)),
) -> Response:
    """Фото товара (size=thumb — миниатюра 240 px для списка, size=full — как сохранено). Нужен заголовок Authorization,
    поэтому страница получает его через fetch -> blob, а не через <img src>."""
    product = db.get(Product, product_id)
    files = list_photo_files(_photos_dir(), product.sku) if product is not None else []
    if not files or not 0 <= n < len(files):
        raise ApiError(404, "product_has_no_photos", "У товара нет фото.")
    source = files[n]
    if size == "full":
        content = source.read_bytes()
    else:
        cached = source.with_name(f"thumb-{source.name[:3]}.jpg")
        if cached.is_file():
            content = cached.read_bytes()
        else:
            content = make_thumbnail(source.read_bytes())
            try:
                cached.write_bytes(content)
            except OSError:
                pass                                   # миниатюра — удобство, не обязанность: без кэша просто пересчитаем
    return Response(content=content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.post("/identify")
def identify_product(
    file: UploadFile,
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(require_permission(Permission.CATALOG_READ)),
) -> dict:
    """«Узнать товар»: один кадр (камера или файл) -> название товара из каталога. Сначала ищем штрих-код на кадре: известный
    код даёт ответ сразу (by="barcode", без модели и воркера — точнее и быстрее). Иначе — распознавание по виду через воркер
    (ждём до 25 секунд — странице не нужно опрашивать статус). Прочитанный, но неизвестный код возвращается в barcode
    (known=false): страница предложит привязать его. Ответ: см. app/services/identify_service.py + demo_mode + by + barcode."""
    data = _read_limited(file, IDENTIFY_MAX_MB * 1024 * 1024, "Кадр")
    try:
        photo = normalize_photo(data, check_quality=False)
    except PhotoError as exc:
        raise from_catalog_error(422, exc) from exc

    try:
        codes = decode_image(load_rgb(photo.jpeg))
    except Exception:  # noqa: BLE001 — чтение штрих-кода не должно ломать распознавание по виду
        codes = []
    owners = _owners(db, codes)
    for code in codes:
        owner = owners.get(code)
        product = db.get(Product, owner[0]) if owner else None
        if product is not None:
            info = {"id": product.id, "name": product.name, "brand": product.brand, "expected_price": product.expected_price, "score": 1.0}
            return {"status": "matched", "by": "barcode", "product": info, "score": 1.0, "candidates": [info], "demo_mode": False,
                    "barcode": {"code": code, "known": True}}

    root = identify_root(_media_root())
    sweep_old_files(root, max_age_seconds=IDENTIFY_KEEP_SECONDS, now=time.time())
    ref = save_identify_frame(root, uuid.uuid4().hex, photo.jpeg)
    task = None
    try:
        # expires: если воркер не взял задание за время ожидания (остановлен, занят), оно не выполнится позже — клиент уже ушёл,
        # а устаревшие кадры не должны задерживать новые после перезапуска воркера.
        task = identify_product_task.apply_async(args=[ref], expires=IDENTIFY_WAIT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(503, "queue_unavailable",
                       "Очередь задач сейчас недоступна. Проверьте, что система запущена (docker compose ps), и повторите.") from exc
    try:
        result = task.get(timeout=IDENTIFY_WAIT_SECONDS, propagate=False)
    except Exception as exc:  # noqa: BLE001 — celery.exceptions.TimeoutError и ошибки бэкенда
        raise ApiError(504, "identify_timeout",
                       "Система не успела ответить. Проверьте, что она запущена и не занята (docker compose ps), и повторите.") from exc
    if isinstance(result, Exception):
        if isinstance(result, AppError) or type(result).__name__ in {"PhotoError", "CatalogAddError", "NotReadyError"}:
            raise from_app_error(result, fallback_code="catalog_error")        # статус по виду: 422, а «не готово» — 503
        raise ApiError(500, "recognize_failed", "Не получилось распознать товар. Попробуйте ещё раз.")
    result["by"] = "look"
    result["barcode"] = {"code": codes[0], "known": False} if codes else None
    return result
