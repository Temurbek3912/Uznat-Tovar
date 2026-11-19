"""
Celery worker: снимает тяжёлую ML-обработку с HTTP-запроса и выполняет её асинхронно. Три задачи:
добавление товара в каталог, добавление фотографий к товару и распознавание товара по фото.

Инициализация ML-компонентов (детектор/эмбеддер/матчер) происходит один
раз на процесс воркера через `@worker_process_init` — а не при каждом
вызове задачи, иначе каждая обработка заново грузила бы веса модели
в память/GPU, что убило бы всю латентность.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time

import numpy as np
from celery import Celery, Task
from celery.signals import worker_init, worker_process_init, worker_ready
from sqlalchemy.orm import Session

from app.core.config import get_settings, resolve_project_path
from app.db.models import PRODUCT_ARCHIVED, Product
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

# Облегчённый режим (LITE_MODE=1, документ 11, §10): задачи выполняются прямо в процессе API (task_always_eager), без Redis и без
# отдельного воркера; результаты для «ход добавления товара» — в памяти процесса. Задачи идут строго по одной (как --pool=solo
# у воркера): одновременные снимки ждут друг друга, а не грузят модель параллельно.
_serial = threading.RLock()


class _SerialInLite(Task):
    def __call__(self, *args, **kwargs):
        if self.app.conf.task_always_eager:
            with _serial:
                return super().__call__(*args, **kwargs)
        return super().__call__(*args, **kwargs)


if settings.lite_mode:
    celery_app = Celery("shelf_audit", broker="memory://", backend="cache+memory://", task_cls=_SerialInLite)
    celery_app.conf.update(task_always_eager=True, task_store_eager_result=True, task_eager_propagates=False)
else:
    celery_app = Celery("shelf_audit", broker=settings.queue_broker_url, backend=settings.queue_broker_url, task_cls=_SerialInLite)
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


def _load_or_create_index(index_path: str | None, embedding_dim: int, model: dict | None = None):
    """Индекс каталога для воркера: app.pipeline.matcher.open_catalog_index (одна реализация со скриптами импорта).

    Файла нет — пустой индекс (нормально до первого товара). Файл есть, но повреждён или от другой модели — исключение:
    build_runtime помечает «matcher» неготовым с причиной, а пустой индекс поверх файла НЕ записывается (раньше так молча
    стирались все эталоны при первом же добавлении товара). model — паспорт модели эмбеддера: индекс другой модели не
    загружается (векторы несравнимы)."""
    index = open_catalog_index(index_path, embedding_dim, model)
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


def _refresh_exclusions(db: Session) -> None:
    """Перед распознаванием: какие векторы индекса пропускать — фото, выведенные из эталонов или сделанные витринными на
    странице «Товары» (база — источник истины; индекс не пересобирается, изменение действует со следующего кадра). Сбой
    чтения базы — остаётся прежний список, распознавание не останавливается."""
    if _matcher is None:
        return
    from app.services.product_images import PhotoRegistry
    try:
        _matcher.excluded = frozenset(PhotoRegistry(db).excluded_sources())
        _matcher.excluded_products = frozenset(
            pid for (pid,) in db.query(Product.id).filter(Product.status == PRODUCT_ARCHIVED).all())
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("Не удалось прочитать выведенные из эталонов фото и товары в архиве: %s", type(exc).__name__)


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


def _link_barcodes(db: Session, product_id: str, barcodes: list[str]) -> list[dict]:
    """Привязывает штрих-коды к только что созданному товару. Товар уже сохранён и проиндексирован, поэтому сбой привязки
    (другой запрос успел занять код) НЕ откатывает и не роняет добавление; непривязанные коды возвращаются, чтобы сказать о них
    человеку (app/services/barcode_link.py)."""
    from app.services.barcode_link import link_barcodes
    return link_barcodes(db, product_id, barcodes)


def _media_root():
    return resolve_project_path(settings.media_root)


def _download_image(url: str, digests: dict | None = None) -> np.ndarray:
    """Эталонное фото товара -> RGB-массив. Источник — одно из двух:
      * «upload:<папка>/<файл>» — фото, загруженное из браузера («Добавить товар»): читается ТОЛЬКО из data/catalog-uploads
        (путь проверяется — «../» и символьные ссылки наружу не пройдут);
      * http(s)-ссылка (прежний способ, POST /catalog/products с JSON) — скачивается.
    Нечитаемое фото — PhotoError с понятным текстом (повтор задачи ему не поможет). digests — сюда SHA-256 скачанного файла
    (для записи о фото в product_images; файл по ссылке программа не хранит)."""
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
    if digests is not None:
        digests[url] = hashlib.sha256(response.content).hexdigest()
    try:
        return load_rgb(response.content)
    except Exception as exc:  # noqa: BLE001
        raise PhotoError("По ссылке лежит не фото или файл повреждён.", code="photo_not_image") from exc


def _photo_infos(refs: list[str], digests: dict):
    """Записи о фото для product_images: фото из браузера — SHA-256 файла и путь; по ссылке — SHA-256 скачанного."""
    from app.services.product_images import PhotoInfo, upload_photo_info
    root = uploads_root(_media_root())
    infos, keep = [], []
    for i, ref in enumerate(refs):
        if is_upload_ref(ref):
            infos.append(upload_photo_info(root, ref))
        elif ref in digests:
            infos.append(PhotoInfo(sha256=digests[ref], source_ref=ref))
        else:
            continue
        keep.append(i)
    return infos, keep


def _remember_photos(db: Session, digests: dict | None = None):
    """Для app/services/catalog_add и index_coverage: записи о фото (product_images) и их векторы в кэш (image_embeddings,
    версия — паспорт модели эмбеддера) — до записи в индекс: база — источник истины, индекс восстанавливается из неё."""
    from app.pipeline.embedder import model_signature
    from app.services.product_images import PhotoRegistry

    def remember(product_id: str, refs: list[str], vectors) -> None:
        infos, keep = _photo_infos(list(refs), digests or {})
        if infos:
            PhotoRegistry(db).remember(product_id, infos, np.atleast_2d(vectors)[keep], model_signature(_embedder))
    return remember


def _backfill_photos(db: Session, products: dict[str, str]) -> None:
    """Перенос существующих фото в product_images (файлы не трогаются) и векторов индекса известной модели в кэш. Сбой не
    мешает восстановлению индекса: повторится при следующем запуске воркера."""
    from app.services.catalog_upload import folder_name, list_photo_files, upload_ref
    from app.services.product_images import PhotoRegistry, backfill
    try:
        res = backfill(PhotoRegistry(db), products, uploads_root(_media_root()), index=_matcher.index, folder_of=folder_name,
                       list_photos=list_photo_files, upload_ref=upload_ref)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Записи о фото товаров не перенесены (повторится при следующем запуске воркера)")
        return
    if res.uploads_registered or res.import_registered or res.vectors_cached:
        logger.info("Фото товаров в базе: записано из папки загрузок %d, из импорта %d; векторов в кэш %d",
                    res.uploads_registered, res.import_registered, res.vectors_cached)
    if res.index_model_unknown:
        logger.warning("Модель векторов индекса не записана: векторы в кэш не перенесены (рекомендуется полная пересборка)")


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
        digests: dict[str, str] = {}
        images = [_download_image(url, digests) for url in product_in.reference_image_urls]

        # База — источник истины: товар фиксируется в базе ДО записи векторов (app/services/catalog_add.py). Повтор задачи
        # находит уже созданный товар по артикулу и добавляет только фото, которых нет в журнале проиндексированных.
        index_path = _index_path()
        outcome = add_product(product_in, images, store=SqlProductStore(db), embedder=_embedder, index=_matcher.index,
                              index_path=index_path,
                              not_indexed=_not_indexed(index_path, None),
                              note_indexed=lambda refs: _note_indexed(index_path, refs),
                              remember=_remember_photos(db, digests))
        not_linked = _link_barcodes(db, outcome.product_id, product_in.barcodes)

        logger.info("%s SKU %s -> product_id=%s (векторов добавлено: %d, уже были: %d)",
                    "Добавлен" if outcome.created else "Уже был в базе (повтор)", product_in.sku, outcome.product_id,
                    outcome.vectors_added, len(outcome.skipped_refs))
        from app.services.barcode_link import task_result
        return task_result(outcome.product_id, not_linked)

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
        digests: dict[str, str] = {}
        images = [_download_image(ref, digests) for ref in todo]
        added, _skipped = index_photos(product.id, todo, images, embedder=_embedder, index=_matcher.index, index_path=index_path,
                                       not_indexed=lambda r: list(r), note_indexed=lambda r: _note_indexed(index_path, r),
                                       remember=_remember_photos(db, digests))
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
    """Автоматическое восстановление индекса (ставится при каждом старте воркера и затем по расписанию — раз в
    recover_index_interval_minutes, см. _schedule_recovery и app/services/recovery_schedule.py).

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
        _backfill_photos(db, products)
        remember_db = _remember_photos(db)

        def remember(pid: str, refs: list[str], vectors) -> None:
            try:
                remember_db(pid, refs, vectors)
            except Exception:  # noqa: BLE001 — запись о фото догонит перенос при следующем запуске; индекс важнее
                db.rollback()
                logger.exception("Записи о фото товара %s не сохранены в базе", pid)

        try:
            res = recover_pending(_matcher.index, products, uploads_root(_media_root()), embedder=_embedder,
                                  index_path=index_path, folder_of=folder_name, list_photos=list_photo_files,
                                  upload_ref=upload_ref, load_image=_load, manifest_keys=_manifest_keys(index_path),
                                  note_indexed=lambda refs: _note_indexed(index_path, refs), limit=RECOVER_BATCH,
                                  remember=remember)
        except Exception as exc:
            _resync_index_after_failure(size_before, exc)
            logger.exception("Восстановление индекса прервано (повторится по расписанию или при следующем запуске воркера)")
            raise
    finally:
        db.close()
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


def _enqueue_recovery(expires: float | None = None) -> None:
    """Поставить задачу восстановления индекса. expires — сколько секунд она может ждать в очереди: не взятая к следующему тику
    расписания отбрасывается, поэтому копии не копятся (в облегчённом режиме задача выполняется сразу, expires не нужен)."""
    if expires:
        recover_index_task.apply_async(expires=expires)
    else:
        recover_index_task.delay()


_recovery_ticker = None      # RecoveryTicker процесса (один на процесс; см. _start_recovery_schedule)


def _start_recovery_schedule() -> bool:
    """Запустить расписание восстановления индекса в этом процессе (один раз). False — выключено настройкой или уже идёт."""
    global _recovery_ticker
    from app.services.recovery_schedule import RecoveryTicker, interval_seconds

    if _recovery_ticker is None:
        _recovery_ticker = RecoveryTicker(interval_seconds(getattr(settings, "recover_index_interval_minutes", 0)),
                                          _enqueue_recovery)
    started = _recovery_ticker.start()
    if started:
        logger.info("Восстановление индекса по расписанию: раз в %.0f мин", _recovery_ticker.interval / 60)
    return started


@worker_ready.connect
def _schedule_recovery(**kwargs) -> None:
    """При каждом запуске воркера — одна задача восстановления индекса, дальше — по расписанию (recover_index_interval_minutes).
    Очередь недоступна — не беда: следующий тик расписания поставит снова; вручную — scripts/reindex_uploads.py. Исключения из
    обработчика сигнала Celery проглотил бы — ловим сами."""
    try:
        _enqueue_recovery()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Задача восстановления индекса не поставлена (%s) — повторится по расписанию", exc)
    try:
        _start_recovery_schedule()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Расписание восстановления индекса не запущено (%s) — только при запуске воркера", exc)


@celery_app.task(name="shelf_audit.identify_product", bind=True)
def identify_product_task(self, ref: str) -> dict:
    """«Узнать товар»: один кадр с камеры -> название товара из каталога. Без повторов (кадр устаревает за секунды: пользователь
    уже показал следующий товар) и с обязательным удалением временного файла. Ответ — словарь из identify_service.identify
    + demo_mode: True, если вместо настоящей модели работает случайная заглушка (тогда ответам верить нельзя)."""
    try:
        _require_ml("embedder", "matcher")
    except MLNotReadyError as exc:
        raise _ml_unavailable(exc) from exc

    from app.core.frame_store import FrameStoreError, frame_id_of, get_frame_store, is_frame_ref
    from app.services.identify_service import ProductInfo, identify

    path = None
    if is_frame_ref(ref):
        # D-10а: кадр — только в памяти (отдельный Redis без сохранения на диск, app/core/frame_store.py). take = прочитать и сразу
        # удалить; дальше кадр живёт только в памяти этого вызова.
        try:
            frame = get_frame_store().take(frame_id_of(ref))
        except FrameStoreError as exc:
            raise PhotoError("Кадр недоступен — попробуйте ещё раз.", code="frame_unavailable") from exc
        if frame is None:
            raise PhotoError("Кадр устарел — сфотографируйте товар ещё раз.", code="frame_expired")
        source = frame
    elif is_identify_ref(ref):
        # Прежний способ (файл в identify-tmp) — только чтобы дочитать задания, поставленные до обновления. Файл удаляется ниже.
        path = resolve_ref(identify_root(_media_root()), IDENTIFY_REF_PREFIX, ref)
        source = path
    else:
        raise PhotoError("Неверная ссылка на кадр.", code="photo_bad_ref")
    db = SessionLocal()
    try:
        try:
            image = load_rgb(source)
        except Exception as exc:  # noqa: BLE001
            raise PhotoError("Кадр не читается — попробуйте ещё раз.", code="frame_unreadable") from exc
        del source

        def lookup(ids: list[str]) -> dict:
            rows = db.query(Product).filter(Product.id.in_(ids), Product.status != PRODUCT_ARCHIVED).all()
            return {r.id: ProductInfo(id=r.id, name=r.name, brand=r.brand, expected_price=r.expected_price) for r in rows}

        _refresh_index()
        _refresh_exclusions(db)
        result = identify(image, _embedder, _matcher, lookup)
        result["demo_mode"] = _runtime is not None and _runtime.component("embedder").status == ml_runtime.DEGRADED
        return result
    finally:
        db.close()
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


def _preload_kind_model() -> None:
    """Облегчённый режим без модели «зрение + текст»: малая модель «вид товара» грузится заранее, а не на первом снимке покупателя
    (документ 07, §10.7)."""
    if not getattr(settings, "naming_enabled", False) or getattr(settings, "naming_vision_url", "")             or not getattr(settings, "naming_kind_model", ""):
        return
    try:
        from app.pipeline.kind_classifier import get_classifier  # noqa: PLC0415
        get_classifier(settings)
    except Exception:  # noqa: BLE001 — вид товара необязателен
        logger.exception("Облегчённый режим: модель «вид товара» не загружена")


def start_in_process() -> None:
    """Облегчённый режим: то, что делает отдельный воркер при запуске, — в процессе API (вызывается в фоновом потоке при старте
    API, app/main.py). Модели грузятся под той же блокировкой, что и задачи: снимок, пришедший во время загрузки, подождёт её,
    а не начнёт вторую загрузку. Ошибки — в журнал и в файл статуса (как у воркера); API продолжает работать (штрихкод)."""
    with _serial:
        ml_runtime.clear_status()
        problems = ml_runtime.preflight(settings, os.environ)
        if problems:
            for problem in problems:
                logger.critical("Распознавание по виду не может стать готовым: %s", problem)
        _init_ml_components()
    _preload_kind_model()
    for job in (_sweep_stale_frames, _schedule_recovery):
        try:
            job()
        except Exception:  # noqa: BLE001 — уборка и восстановление индекса не должны мешать работе
            logger.exception("Облегчённый режим: шаг запуска %s не выполнен", job.__name__)

