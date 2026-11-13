"""
Celery worker: снимает тяжёлую ML-обработку с HTTP-запроса и выполняет её асинхронно. Три задачи:
добавление товара в каталог, добавление фотографий к товару и распознавание товара по фото.

Инициализация ML-компонентов (детектор/эмбеддер/матчер) происходит один
раз на процесс воркера через `@worker_process_init` — а не при каждом
вызове задачи, иначе каждая обработка заново грузила бы веса модели
в память/GPU, что убило бы всю латентность.
"""
from __future__ import annotations

import logging
import os
import time

import numpy as np
from celery import Celery
from celery.signals import worker_init, worker_process_init, worker_ready
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_project_path
from app.db.models import Product, ProductBarcodeORM
from app.db.session import SessionLocal
from app.pipeline.detector import ProductDetector
from app.pipeline.embedder import ImageEmbedder
from app.pipeline.imageio import load_rgb
from app.pipeline.matcher import (IndexIntegrityError, ProductMatcher, index_size, open_catalog_index, refresh_index_if_changed,
                                  resync_index)
from app.pipeline import ml_runtime
from app.pipeline.ml_runtime import MLNotReadyError
from app.pipeline.privacy import FaceBlurrer
from app.services.catalog_upload import (IDENTIFY_REF_PREFIX, CatalogAddError, PhotoError, identify_root, is_identify_ref,
                                         is_upload_ref, not_ready_error, record_indexed, resolve_ref, resolve_upload_ref,
                                         uploads_root)

logger = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery("shelf_audit", broker=settings.queue_broker_url, backend=settings.queue_broker_url)
celery_app.conf.update(task_acks_late=True, worker_prefetch_multiplier=1)

# Заполняется в _init_ml_components() один раз на процесс воркера.
_detector: ProductDetector | None = None
_embedder: ImageEmbedder | None = None
_matcher: ProductMatcher | None = None
_face_blurrer: FaceBlurrer | None = None


_runtime: ml_runtime.MLRuntime | None = None
_last_reinit = 0.0
REINIT_INTERVAL_SECONDS = 60.0      # повторная попытка поднять компоненты из задачи — не чаще раза в минуту


def _apply_runtime(rt: ml_runtime.MLRuntime) -> None:
    global _detector, _embedder, _matcher, _face_blurrer
    _detector, _embedder, _matcher = rt.detector, rt.embedder, rt.matcher
    _face_blurrer = rt.face_blurrer


@worker_init.connect
def _preflight_worker(**kwargs) -> None:
    """Главный процесс воркера, ДО пула: если деталь, без которой система не работает, неисправна (OpenCV без CascadeClassifier,
    нет файла весов детектора) — воркер не должен объявлять «ready». SystemExit — BaseException: Celery при рассылке сигнала
    перехватывает только Exception («Signal handler raised» и работа дальше), поэтому запуск здесь действительно прервётся."""
    ml_runtime.clear_status()                      # статус прошлого запуска не должен выдавать себя за текущий
    problems = ml_runtime.preflight(settings, os.environ)
    if problems:
        for problem in problems:
            logger.critical("Воркер не может стать готовым: %s", problem)
        logger.critical("Воркер остановлен до объявления готовности. Исправьте причины выше (docs/RUNBOOK.md) и запустите снова.")
        raise SystemExit(78)


@worker_process_init.connect
def _init_ml_components(**kwargs) -> None:
    """Поднимает компоненты распознавания (см. app/pipeline/ml_runtime.py). НЕ бросает исключений: Celery их всё равно проглотил бы
    и объявил готовность. Итог — таблица в логе и файл статуса для healthcheck; задачи перед работой вызывают _require_ml()."""
    global _runtime
    _runtime = ml_runtime.build_runtime(settings, os.environ)
    _apply_runtime(_runtime)
    for line in _runtime.summary_lines():
        (logger.info if _runtime.ready else logger.critical)("%s", line)
    ml_runtime.write_status(_runtime)


def _require_ml(*names: str) -> None:
    """Задача не начинает работу, если нужный обязательный компонент не готов: MLNotReadyError с причинами — вместо «успешного»
    ответа, которого не было. Не готов — раз в минуту пробуем поднять заново (появился интернет, положили веса)."""
    global _last_reinit
    if _runtime is None or not _runtime.ready:
        now = time.monotonic()
        if _runtime is None or now - _last_reinit >= REINIT_INTERVAL_SECONDS:
            _last_reinit = now
            _init_ml_components()
    if _runtime is None:
        raise MLNotReadyError(["компоненты распознавания не инициализированы"])
    _runtime.require(*names)


def _ml_unavailable(exc: MLNotReadyError) -> CatalogAddError:
    """Понятное человеку сообщение (уйдёт на страницу как причина отказа, а не как «Не получилось»). Код ml_not_ready, вид
    unavailable (HTTP 503). Клиенту — только имена компонентов; причины (тексты исключений, пути к весам и кешу) — в журнал."""
    logger.error("Распознавание не готово: %s", "; ".join(exc.problems))
    return not_ready_error(exc.problems)


def _load_or_create_index(index_path: str | None, embedding_dim: int):
    """Индекс каталога для воркера: app.pipeline.matcher.open_catalog_index (одна реализация со скриптами импорта).

    Файла нет — пустой индекс (нормально до первого товара). Файл есть, но повреждён или от другой модели — исключение:
    build_runtime помечает «matcher» неготовым с причиной, а пустой индекс поверх файла НЕ записывается (раньше так молча
    стирались все эталоны при первом же добавлении товара)."""
    index = open_catalog_index(index_path, embedding_dim)
    logger.info("Индекс каталога: %s (%d векторов)", index_path or "в памяти", len(index._ids))
    return index


def _index_path() -> str | None:
    return settings.matcher.index_path if settings.matcher else None


def _index_dim() -> int:
    return int(getattr(_embedder, "embedding_dim", 64))


def _refresh_index() -> None:
    """Перед работой с индексом: если его записал другой процесс (импорт каталога), загрузить заново — иначе распознавание
    не видит новых товаров, а сохранение воркера было бы отклонено как перезапись чужих изменений."""
    if _matcher is None:
        return
    status = refresh_index_if_changed(_matcher, _index_path(), _index_dim())
    if status.startswith("error"):
        logger.error("Индекс каталога изменён другим процессом и не загружается: %s", status)


def _resync_index_after_failure(size_before: int, exc: BaseException) -> None:
    """Задача упала после того, как векторы уже попали в индекс в памяти (сохранение или база не удались), либо индекс на
    диске оказался чужим. Индекс в памяти заменяется сохранённым на диске: иначе несохранённые векторы (товара, которого нет
    в базе) попали бы на диск со следующим удачным сохранением, а повтор задачи добавил бы их ещё раз."""
    if _matcher is None or (index_size(_matcher.index) == size_before and not isinstance(exc, IndexIntegrityError)):
        return
    error = resync_index(_matcher, _index_path(), _index_dim())
    if error is None:
        logger.warning("Индекс каталога перечитан с диска после сбоя задачи (%s)", type(exc).__name__)
        return
    logger.critical("Индекс каталога в памяти расходится с диском, а с диска он не загружается: %s", error)
    if _runtime is not None:                                    # пользоваться индексом в памяти нельзя: «matcher» — не готов
        comp = _runtime.component("matcher")
        comp.status, comp.error = ml_runtime.FAILED, f"индекс не загружается после сбоя: {error}"
        ml_runtime.write_status(_runtime)


def _manifest_keys(index_path: str | None) -> set[str]:
    """Отметки журнала uploads_manifest.json — нужны только для товаров со старыми векторами без записи об источнике."""
    if not index_path:
        return set()
    from app.services.catalog_upload import UploadsManifest, manifest_path_for
    try:
        return set(UploadsManifest.load(manifest_path_for(index_path)).keys)
    except (CatalogAddError, OSError):
        return set()


def _not_indexed(index_path: str | None, product_id: str | None):
    """Какие фото ещё без векторов — решает сам индекс (у каждого вектора записано его фото; app/services/index_coverage.py)."""
    from app.services.index_coverage import not_indexed_filter
    return not_indexed_filter(_matcher.index, _manifest_keys(index_path), product_id)


def _note_indexed(index_path: str | None, refs: list[str]) -> None:
    """Запоминает, какие загруженные фото уже в индексе (журнал рядом с индексом) — для восстановления после пересборки индекса
    (scripts/reindex_uploads.py). Только ссылки upload:… (фото из браузера); сбой журнала не роняет добавление товара."""
    if not index_path:
        return
    uploaded = [r for r in refs if is_upload_ref(r)]
    if uploaded and not record_indexed(index_path, uploaded):
        logger.warning("Не удалось записать журнал проиндексированных фото рядом с %s — scripts/reindex_uploads.py доиндексирует их", index_path)


def _link_barcodes(db: Session, product_id: str, barcodes: list[str]) -> None:
    """Привязывает штрих-коды к только что созданному товару. Товар уже сохранён и проиндексирован, поэтому сбой привязки
    (другой запрос успел занять код) НЕ откатывает и не роняет добавление: пишем в лог, остальные коды привязываем."""
    for code in barcodes:
        try:
            db.add(ProductBarcodeORM(barcode=code, product_id=product_id))
            db.commit()
        except Exception:  # noqa: BLE001 — IntegrityError: код уже занят; любая иная — тоже не повод терять товар
            db.rollback()
            logger.warning("Штрих-код %s не привязан к товару %s (уже занят или ошибка БД)", code, product_id)


def _media_root():
    return resolve_project_path(settings.media_root)


def _download_image(url: str) -> np.ndarray:
    """Эталонное фото товара -> RGB-массив. Источник — одно из двух:
      * «upload:<папка>/<файл>» — фото, загруженное из браузера («Добавить товар»): читается ТОЛЬКО из data/catalog-uploads
        (путь проверяется — «../» и символьные ссылки наружу не пройдут);
      * http(s)-ссылка (прежний способ, POST /catalog/products с JSON) — скачивается.
    Нечитаемое фото — PhotoError с понятным текстом (повтор задачи ему не поможет)."""
    if is_upload_ref(url):
        source = resolve_upload_ref(uploads_root(_media_root()), url)
        try:
            return load_rgb(source)
        except Exception as exc:  # noqa: BLE001 — любой отказ декодера = файл непригоден
            raise PhotoError("Фото повреждено и не читается — загрузите другое.", code="photo_corrupted") from exc

    import httpx

    with httpx.Client(timeout=15.0) as client:
        response = client.get(url)
        response.raise_for_status()
    try:
        return load_rgb(response.content)
    except Exception as exc:  # noqa: BLE001
        raise PhotoError("По ссылке лежит не фото или файл повреждён.", code="photo_not_image") from exc


# БАГФИКС (вычитка после F12): декоратор был потерян при вставке соседней задачи выше, и
# функция превратилась в обычную — routes_catalog.py вызывал бы .delay() у функции (AttributeError, 500).
# Тесты это не ловили, т.к. подменяют задачу заглушкой; см. tests/test_worker_static.py.
@celery_app.task(name="shelf_audit.add_catalog_product", bind=True, max_retries=3, default_retry_delay=15)
def add_catalog_product_task(self, product_in_dict: dict) -> str:
    """
    Реализация F10 из ТЗ: добавление нового SKU без переобучения моделей.
    Выполняется в Celery, а не синхронно в API-запросе, по двум причинам:
      (1) эмбеддер уже загружен в память этого процесса (_init_ml_components),
          повторная загрузка модели на каждый HTTP-запрос была бы неприемлемо
          медленной;
      (2) скачивание эталонных фото по сети не должно блокировать API-воркер.
    """
    try:
        _require_ml("embedder", "matcher")
    except MLNotReadyError as exc:
        raise _ml_unavailable(exc) from exc

    from app.core.schemas import ProductIn
    from app.services.catalog_add import add_product
    from app.services.catalog_service import SqlProductStore

    product_in = ProductIn(**product_in_dict)
    _refresh_index()
    size_before = index_size(_matcher.index)
    db = SessionLocal()
    try:
        images = [_download_image(url) for url in product_in.reference_image_urls]

        # База — источник истины: товар фиксируется в базе ДО записи векторов (app/services/catalog_add.py). Повтор задачи
        # находит уже созданный товар по артикулу и добавляет только фото, которых нет в журнале проиндексированных.
        index_path = _index_path()
        outcome = add_product(product_in, images, store=SqlProductStore(db), embedder=_embedder, index=_matcher.index,
                              index_path=index_path,
                              not_indexed=_not_indexed(index_path, None),
                              note_indexed=lambda refs: _note_indexed(index_path, refs))
        _link_barcodes(db, outcome.product_id, product_in.barcodes)

        logger.info("%s SKU %s -> product_id=%s (векторов добавлено: %d, уже были: %d)",
                    "Добавлен" if outcome.created else "Уже был в базе (повтор)", product_in.sku, outcome.product_id,
                    outcome.vectors_added, len(outcome.skipped_refs))
        return outcome.product_id

    except CatalogAddError as exc:
        db.rollback()
        _resync_index_after_failure(size_before, exc)
        raise          # понятная человеку причина (плохое фото…) — повтор не поможет, показываем её сразу
    except Exception as exc:
        db.rollback()
        _resync_index_after_failure(size_before, exc)
        logger.exception("Ошибка добавления SKU %s в каталог", product_in_dict.get("sku"))
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="shelf_audit.add_catalog_photos", bind=True, max_retries=3, default_retry_delay=15)
def add_catalog_photos_task(self, product_id: str, refs: list[str]) -> str:
    """Добавляет эталонные фото К УЖЕ СУЩЕСТВУЮЩЕМУ товару («+ фото» на странице «Товары»): чем больше ракурсов, тем увереннее
    узнаёт. Вектора только ДОБАВЛЯЮТСЯ в индекс (удалять он не умеет), запись товара в БД не меняется. Возвращает product_id."""
    try:
        _require_ml("embedder", "matcher")
    except MLNotReadyError as exc:
        raise _ml_unavailable(exc) from exc

    from app.services.catalog_add import index_photos

    _refresh_index()
    size_before = index_size(_matcher.index)
    db = SessionLocal()
    try:
        product = db.get(Product, product_id)
        if product is None:
            raise CatalogAddError("Товар не найден — возможно, его уже убрали из каталога.", code="product_not_found")
        index_path = _index_path()
        todo = _not_indexed(index_path, product.id)(list(refs))
        if not todo:                     # повтор задачи после удачного сохранения или то же фото ещё раз: векторы уже есть
            logger.info("Фото товара %s уже в индексе — повторно не добавляются", product.sku)
            return product.id
        images = [_download_image(ref) for ref in todo]
        added, _skipped = index_photos(product.id, todo, images, embedder=_embedder, index=_matcher.index, index_path=index_path,
                                       not_indexed=lambda r: list(r), note_indexed=lambda r: _note_indexed(index_path, r))
        logger.info("К SKU %s добавлено %d эталонных фото (product_id=%s)", product.sku, added, product.id)
        return product.id
    except CatalogAddError as exc:
        _resync_index_after_failure(size_before, exc)
        raise
    except Exception as exc:
        _resync_index_after_failure(size_before, exc)
        logger.exception("Ошибка добавления фото к товару %s", product_id)
        raise self.retry(exc=exc)
    finally:
        db.close()


RECOVER_BATCH = 200          # фото за один запуск восстановления; дальше задача ставит себя снова, пока есть продвижение


@celery_app.task(name="shelf_audit.recover_index", bind=True)
def recover_index_task(self) -> dict:
    """Автоматическое восстановление индекса (запускается при каждом старте воркера, см. _schedule_recovery).

    Товар записывается в базу ДО векторов (app/services/catalog_add.py). Если после этого процесс погиб, а задача очереди
    потерялась, в базе остаётся товар, у фото которого (data/catalog-uploads) нет векторов. Источник истины здесь — база
    и файлы фото, а «есть ли вектор» решает сам индекс, поэтому отдельная очередь заданий (outbox) не нужна: недостающее
    вычисляется заново при каждом запуске. Повтор безопасен — дублей нет. Фото, которые не читаются, — в журнал; товар без
    фото восстановить нечем (виден в scripts/reconcile_index.py)."""
    try:
        _require_ml("embedder", "matcher")
    except MLNotReadyError as exc:
        logger.warning("Восстановление индекса отложено: %s", exc)
        return {"added": 0, "failed": 0, "remaining": None, "skipped": "ml not ready"}

    from app.pipeline.imageio import load_rgb as _load
    from app.services.catalog_upload import folder_name, list_photo_files, upload_ref
    from app.services.index_coverage import recover_pending

    _refresh_index()
    size_before = index_size(_matcher.index)
    index_path = _index_path()
    db = SessionLocal()
    try:
        products = {pid: sku for pid, sku in db.query(Product.id, Product.sku).all()}
    finally:
        db.close()
    try:
        res = recover_pending(_matcher.index, products, uploads_root(_media_root()), embedder=_embedder, index_path=index_path,
                              folder_of=folder_name, list_photos=list_photo_files, upload_ref=upload_ref, load_image=_load,
                              manifest_keys=_manifest_keys(index_path), note_indexed=lambda refs: _note_indexed(index_path, refs),
                              limit=RECOVER_BATCH)
    except Exception as exc:
        _resync_index_after_failure(size_before, exc)
        logger.exception("Восстановление индекса прервано (повторится при следующем запуске воркера)")
        raise
    for pid, ref, why in res.failed:
        logger.warning("Восстановление индекса: фото %s товара %s не читается: %s", ref, pid, why)
    if res.added:
        logger.info("Восстановление индекса: добавлены векторы %d фото", len(res.added))
    if res.remaining and res.progressed:                # продолжение партиями; без продвижения не зацикливается
        recover_index_task.delay()
    return {"added": len(res.added), "failed": len(res.failed), "remaining": res.remaining}


@worker_ready.connect
def _sweep_stale_frames(**kwargs) -> None:
    """При запуске воркера — удалить временные кадры старше 2 минут, оставшиеся от прошлого запуска (ТК-2). Основную уборку
    ведёт API (app/main.py, lifespan); это страховка на случай, когда API не работал."""
    try:
        from app.services.frame_lifecycle import sweep_frames

        removed = sweep_frames(_media_root())
        if removed:
            logger.info("При запуске удалено устаревших временных кадров: %d", removed)
    except Exception as exc:  # noqa: BLE001 — исключения из обработчика сигнала Celery проглотил бы
        logger.warning("Уборка временных кадров при запуске не удалась (%s)", exc)


@worker_ready.connect
def _schedule_recovery(**kwargs) -> None:
    """При каждом запуске воркера — одна задача восстановления индекса. Очередь недоступна — не беда: восстановится при
    следующем запуске или вручную (scripts/reindex_uploads.py). Исключения из обработчика сигнала Celery проглотил бы — ловим сами."""
    try:
        recover_index_task.delay()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Задача восстановления индекса не поставлена (%s) — повторится при следующем запуске воркера", exc)


@celery_app.task(name="shelf_audit.identify_product", bind=True)
def identify_product_task(self, ref: str) -> dict:
    """«Узнать товар»: один кадр с камеры -> название товара из каталога. Без повторов (кадр устаревает за секунды: пользователь
    уже показал следующий товар) и с обязательным удалением временного файла. Ответ — словарь из identify_service.identify
    + demo_mode: True, если вместо настоящей модели работает случайная заглушка (тогда ответам верить нельзя)."""
    try:
        _require_ml("embedder", "matcher")
    except MLNotReadyError as exc:
        raise _ml_unavailable(exc) from exc

    from app.services.identify_service import ProductInfo, identify

    if not is_identify_ref(ref):
        raise PhotoError("Неверная ссылка на кадр.", code="photo_bad_ref")
    path = resolve_ref(identify_root(_media_root()), IDENTIFY_REF_PREFIX, ref)
    db = SessionLocal()
    try:
        try:
            image = load_rgb(path)
        except Exception as exc:  # noqa: BLE001
            raise PhotoError("Кадр не читается — попробуйте ещё раз.", code="frame_unreadable") from exc

        def lookup(ids: list[str]) -> dict:
            rows = db.query(Product).filter(Product.id.in_(ids)).all()
            return {r.id: ProductInfo(id=r.id, name=r.name, brand=r.brand, expected_price=r.expected_price) for r in rows}

        _refresh_index()
        result = identify(image, _embedder, _matcher, lookup)
        result["demo_mode"] = _runtime is not None and _runtime.component("embedder").status == ml_runtime.DEGRADED
        return result
    finally:
        db.close()
        try:
            path.unlink()
        except OSError:
            pass
