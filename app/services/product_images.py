"""
Фото товаров в базе (product_images) и кэш эмбеддингов (image_embeddings) — шаг Р-6; документ 06, §4.1; документ 05, §13.3;
документ 07, §8.

Что решено:
  * Фото — запись в базе: товар, SHA-256 содержимого (UNIQUE (product_id, sha256) — одно фото у товара один раз), роль
    (reference — эталон для распознавания, display — витрина), активность, источник вектора в индексе (source_ref) и путь
    файла внутри media_root (storage_path; None — файл вне программы: папка импорта, ссылка).
  * Вектор фото хранится в кэше по ключу (фото, версия модели) — ключ идемпотентности: одно фото одной моделью считается один
    раз. Версия — SHA-256 паспорта модели (app/pipeline/embedder.model_version), сам паспорт — в embedding_models: по нему
    индекс пересобирается из кэша без загрузки модели.
  * Индекс — производные данные: rebuild_from_cache строит его заново из активных эталонов и кэша; модель нужна только для
    фото без вектора в кэше.
  * Перенос существующих фото (backfill) создаёт записи для файлов data/catalog-uploads и для фото импорта из индекса.
    Файлы НЕ удаляются и НЕ перемещаются. Векторы из индекса попадают в кэш только если модель индекса известна (манифест
    индекса, 2026-11-14): векторы «непроверенного» индекса неизвестной модели в кэш не переносятся.
Записи и кэш ведутся при добавлении фото (воркер, импорт каталога, scripts/reindex_uploads.py) ДО записи векторов в индекс:
база — источник истины, индекс можно восстановить из неё.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np

from app.pipeline.embedder import model_version

logger = logging.getLogger(__name__)

REFERENCE, DISPLAY = "reference", "display"
ROLES = (REFERENCE, DISPLAY)


# --------------------------------------------------------------------------- чистые помощники


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)      # как app.core.security.utcnow: время UTC без пояса


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


IMPORTED_SUBDIR = "catalog-imported"      # внутри media_root: копии фото импорта каталога (для показа покупателю и пересборки)


def keep_import_copy(media_root: Path, folder: str, source: Path, sha256: str) -> str | None:
    """Копия фото импорта внутри программы — байт в байт (SHA-256 записи о фото остаётся верным): media_root/catalog-imported/
    <папка товара>/<первые 16 знаков SHA-256>.<расширение>. Возвращает storage_path (путь внутри media_root) или None, если
    содержимое файла не совпало с SHA-256 записи. Папку импорта не трогает; уже скопированное второй раз не пишет.
    Папку catalog-imported не просматривает ни перенос фото, ни сверка индекса: копия не становится «новым фото»."""
    import os

    source = Path(source)
    suffix = source.suffix.lower() if source.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
    rel = f"{IMPORTED_SUBDIR}/{folder}/{sha256[:16]}{suffix}"
    dest = Path(media_root) / rel
    if dest.is_file() and sha256_file(dest) == sha256:
        return rel
    if sha256_file(source) != sha256:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(source.read_bytes())
    os.replace(tmp, dest)
    return rel


def vector_to_bytes(vector) -> bytes:
    return np.asarray(vector, dtype="<f4").ravel().tobytes()


def vector_from_bytes(data: bytes, dim: int) -> np.ndarray:
    vec = np.frombuffer(data, dtype="<f4")
    if vec.shape != (dim,):
        raise ValueError(f"вектор в кэше повреждён: {vec.size} чисел вместо {dim}")
    return vec.astype(np.float32)


def image_size(path: Path) -> tuple[int | None, int | None]:
    """Ширина и высота по заголовку файла (без раскрытия всего фото); не читается — (None, None)."""
    try:
        from PIL import Image
        with Image.open(path) as probe:  # exif-ok: только размер для записи о фото
            return int(probe.width), int(probe.height)
    except Exception:  # noqa: BLE001 — размер необязателен
        return None, None


@dataclass(frozen=True)
class PhotoInfo:
    sha256: str
    source_ref: str | None = None
    storage_path: str | None = None
    original_name: str | None = None
    width: int | None = None
    height: int | None = None
    role: str = REFERENCE
    credit: str | None = None          # подпись источника фото (лицензия открытой базы требует указать автора)


def upload_photo_info(uploads_root: Path, ref: str) -> PhotoInfo:
    """Запись о фото из браузера по ссылке «upload:<папка>/<файл>»: SHA-256 файла и путь внутри media_root."""
    from app.services.catalog_upload import UPLOAD_SUBDIR, resolve_upload_ref
    path = resolve_upload_ref(uploads_root, ref)
    width, height = image_size(path)
    folder, name = path.parent.name, path.name
    return PhotoInfo(sha256=sha256_file(path), source_ref=ref, storage_path=f"{UPLOAD_SUBDIR}/{folder}/{name}",
                     original_name=name, width=width, height=height)


# --------------------------------------------------------------------------- база


class PhotoRegistry:
    """Записи о фото и кэш векторов на SQLAlchemy. Каждый метод фиксирует свою работу (commit); при нарушении уникальности
    (одновременная запись того же фото) берёт уже записанное — повтор безопасен."""

    def __init__(self, db):
        self.db = db

    # модели ---------------------------------------------------------------
    def ensure_model(self, signature: dict | None) -> str | None:
        from sqlalchemy.exc import IntegrityError

        from app.db.models import EmbeddingModel
        version = model_version(signature)
        if version is None:
            return None
        if self.db.get(EmbeddingModel, version) is None:
            self.db.add(EmbeddingModel(version=version, created_at=_utcnow(),
                                       signature_json=json.dumps(signature, ensure_ascii=False, sort_keys=True)))
            try:
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
        return version

    def signature_of(self, version: str) -> dict | None:
        from app.db.models import EmbeddingModel
        row = self.db.get(EmbeddingModel, version)
        return json.loads(row.signature_json) if row is not None else None

    def model_versions(self) -> list[str]:
        from app.db.models import EmbeddingModel
        return [v for (v,) in self.db.query(EmbeddingModel.version).order_by(EmbeddingModel.created_at).all()]

    # фото -----------------------------------------------------------------
    def _find(self, product_id: str, sha256: str):
        from app.db.models import ProductImage
        return (self.db.query(ProductImage).filter(ProductImage.product_id == product_id, ProductImage.sha256 == sha256)
                .one_or_none())

    @staticmethod
    def _fill_missing(row, info: PhotoInfo) -> None:
        for attr in ("source_ref", "storage_path", "original_name", "width", "height", "credit"):
            if getattr(row, attr) is None and getattr(info, attr) is not None:
                setattr(row, attr, getattr(info, attr))

    def _stage(self, product_id: str, info: PhotoInfo):
        from app.db.models import ProductImage
        if info.role not in ROLES:
            raise ValueError(f"неизвестная роль фото: {info.role!r}")
        row = self._find(product_id, info.sha256)
        if row is None:
            row = ProductImage(product_id=product_id, sha256=info.sha256, role=info.role, is_active=True,
                               source_ref=info.source_ref, storage_path=info.storage_path, original_name=info.original_name,
                               width=info.width, height=info.height, credit=info.credit, created_at=_utcnow())
            self.db.add(row)
            self.db.flush()
        else:
            self._fill_missing(row, info)
        return row

    def _stage_vector(self, image_id: str, version: str, vector) -> bool:
        from app.db.models import ImageEmbedding
        if self.db.get(ImageEmbedding, (image_id, version)) is not None:
            return False
        vec = np.asarray(vector, dtype=np.float32).ravel()
        self.db.add(ImageEmbedding(image_id=image_id, model_version=version, dim=int(vec.size), vector=vector_to_bytes(vec),
                                   created_at=_utcnow()))
        return True

    def register(self, product_id: str, info: PhotoInfo) -> str:
        """Запись о фото (найти по (товар, SHA-256) или создать); возвращает id записи."""
        return self.remember(product_id, [info], None, None)[0]

    def remember(self, product_id: str, infos: Sequence[PhotoInfo], vectors, signature: dict | None) -> list[str]:
        """Записи о фото товара и (если есть паспорт модели) их векторы в кэш — одной фиксацией; при гонке — по одному."""
        from sqlalchemy.exc import IntegrityError
        if vectors is not None and len(np.atleast_2d(vectors)) != len(infos):
            raise ValueError("число векторов не совпадает с числом фото")
        version = self.ensure_model(signature) if vectors is not None else None
        rows = np.atleast_2d(vectors) if vectors is not None else [None] * len(infos)
        try:
            ids = [self._stage(product_id, info).id for info in infos]
            if version is not None:
                for image_id, vec in zip(ids, rows, strict=True):
                    self._stage_vector(image_id, version, vec)
            self.db.commit()
            return ids
        except IntegrityError:
            self.db.rollback()
        ids = []                                                 # одновременная запись того же фото: по одному
        for info, vec in zip(infos, rows, strict=True):
            for _attempt in range(2):
                try:
                    image_id = self._stage(product_id, info).id
                    if version is not None:
                        self._stage_vector(image_id, version, vec)
                    self.db.commit()
                    break
                except IntegrityError:
                    self.db.rollback()
            else:
                raise RuntimeError(f"не удалось записать фото {info.sha256[:12]} товара {product_id}")
            ids.append(image_id)
        return ids

    def store_vectors(self, pairs: Iterable[tuple[str, np.ndarray]], signature: dict) -> int:
        """Векторы уже записанных фото — в кэш (то, что уже есть для этой версии, не перезаписывается)."""
        from sqlalchemy.exc import IntegrityError
        version = self.ensure_model(signature)
        if version is None:
            return 0
        pairs = list(pairs)
        try:
            added = sum(self._stage_vector(image_id, version, vec) for image_id, vec in pairs)
            self.db.commit()
            return added
        except IntegrityError:
            self.db.rollback()
        added = 0
        for image_id, vec in pairs:
            try:
                added += self._stage_vector(image_id, version, vec)
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
        return added

    def set_active(self, image_id: str, active: bool) -> None:
        """Вывести фото из эталонов (или вернуть). Запись и файл остаются."""
        self.change(image_id, active=active)
        self.db.commit()

    def change(self, image_id: str, *, active: bool | None = None, role: str | None = None,
               product_id: str | None = None) -> tuple[object, dict, dict]:
        """Изменить активность и (или) роль фото БЕЗ фиксации — вызывающий добавляет запись журнала и делает commit в той же
        транзакции. Возвращает (запись, было, стало) — только изменившиеся поля. product_id — фото должно принадлежать этому
        товару (иначе LookupError, как и для несуществующего фото). Файл и запись не удаляются никогда."""
        from app.db.models import ProductImage
        row = self.db.get(ProductImage, image_id)
        if row is None or (product_id is not None and row.product_id != product_id):
            raise LookupError(image_id)
        if role is not None and role not in ROLES:
            raise ValueError(f"неизвестная роль фото: {role!r}")
        before = {"active": bool(row.is_active), "role": row.role}
        if active is not None and bool(active) != bool(row.is_active):
            row.is_active = bool(active)
            row.deactivated_at = None if active else _utcnow()
        if role is not None:
            row.role = role
        after = {"active": bool(row.is_active), "role": row.role}
        keys = [k for k in after if before[k] != after[k]]
        return row, {k: before[k] for k in keys}, {k: after[k] for k in keys}

    def excluded_sources(self) -> set[tuple[str, str]]:
        """(товар, источник вектора) фото, которые НЕ участвуют в распознавании: выведенные из эталонов и витринные. Векторы
        таких фото могут оставаться в индексе (индекс — производные данные, пересборка необязательна): распознавание их
        пропускает (ProductMatcher.excluded). Если у того же товара с тем же источником есть активный эталон — не исключается."""
        from app.db.models import ProductImage
        rows = self.db.query(ProductImage.product_id, ProductImage.source_ref, ProductImage.role, ProductImage.is_active).filter(
            ProductImage.source_ref.isnot(None)).all()
        used = {(pid, src) for pid, src, role, active in rows if role == REFERENCE and active}
        return {(pid, src) for pid, src, role, active in rows if not (role == REFERENCE and active)} - used

    def summaries(self, product_ids: Sequence[str]) -> dict[str, "PhotoSummary"]:
        """Сводка фото для списка товаров одним запросом (по частям — предел числа параметров): {товар: PhotoSummary}.
        Товара без записей в ответе нет."""
        from app.db.models import ProductImage
        ids = list(dict.fromkeys(product_ids))
        rows: list = []
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            rows += (self.db.query(ProductImage).filter(ProductImage.product_id.in_(part))
                     .order_by(ProductImage.product_id, ProductImage.created_at, ProductImage.id).all())
        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row.product_id, []).append(row)
        return {pid: summarize(images) for pid, images in grouped.items()}

    def images_of(self, product_id: str) -> list:
        from app.db.models import ProductImage
        return (self.db.query(ProductImage).filter(ProductImage.product_id == product_id)
                .order_by(ProductImage.created_at, ProductImage.id).all())

    # чтение ---------------------------------------------------------------
    def known_source_refs(self) -> set[str]:
        from app.db.models import ProductImage
        return {r for (r,) in self.db.query(ProductImage.source_ref).filter(ProductImage.source_ref.isnot(None)).all()}

    def images(self, product_ids: Iterable[str] | None = None) -> list:
        from app.db.models import ProductImage
        rows = self.db.query(ProductImage).order_by(ProductImage.product_id, ProductImage.created_at, ProductImage.id).all()
        if product_ids is None:
            return rows
        wanted = set(product_ids)            # фильтр здесь, а не IN (…): у SQLite предел числа параметров запроса
        return [r for r in rows if r.product_id in wanted]

    def cached_vectors(self, version: str) -> dict[str, np.ndarray]:
        from app.db.models import ImageEmbedding
        rows = self.db.query(ImageEmbedding.image_id, ImageEmbedding.dim, ImageEmbedding.vector).filter(
            ImageEmbedding.model_version == version).all()
        return {image_id: vector_from_bytes(data, dim) for image_id, dim, data in rows}


# --------------------------------------------------------------------------- сводка для каталога (шаг Р-7)


def in_recognition(image) -> bool:
    """Участвует ли фото в распознавании: только активный эталон."""
    return image.role == REFERENCE and bool(image.is_active)


def cover_image(images: Sequence):
    """Какое фото показывать миниатюрой товара: первое активное витринное, иначе первый активный эталон — из тех, у которых
    есть файл в программе (storage_path). Выведенные фото (is_active = False: «не товар», штрихкод и т. п.) миниатюрой
    не бывают никогда: нет активных фото с файлом — None, и страницы показывают заглушку «без фото»."""
    with_file = [i for i in images if i.storage_path and i.is_active]
    for pick in (lambda i: i.role == DISPLAY, lambda i: i.role == REFERENCE):
        for image in with_file:
            if pick(image):
                return image
    return None


@dataclass(frozen=True)
class PhotoSummary:
    total: int                     # все записи о фото товара (включая выведенные и витринные)
    references: int                # активные эталоны — участвуют в распознавании
    display: int                   # витринные
    inactive: int                  # выведенные из эталонов
    cover_id: str | None           # фото для миниатюры (cover_image)
    cover_credit: str | None = None  # чьё это фото (подпись под ним; None — своё фото магазина)


def summarize(images: Sequence) -> PhotoSummary:
    cover = cover_image(images)
    return PhotoSummary(total=len(images), references=sum(1 for i in images if in_recognition(i)),
                        display=sum(1 for i in images if i.role == DISPLAY), inactive=sum(1 for i in images if not i.is_active),
                        cover_id=cover.id if cover is not None else None,
                        cover_credit=getattr(cover, "credit", None) if cover is not None else None)


def photo_file(media_root: Path, image) -> Path | None:
    """Файл фото внутри media_root по storage_path; путь вне media_root, отсутствующий файл или фото вне программы — None."""
    if image is None or not image.storage_path:
        return None
    root = Path(media_root).resolve()
    path = (root / image.storage_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


# --------------------------------------------------------------------------- перенос существующих фото


@dataclass
class BackfillResult:
    uploads_registered: int = 0
    import_registered: int = 0
    vectors_cached: int = 0
    unreadable: list[tuple[str, str]] = field(default_factory=list)      # (ссылка, причина)
    file_sources_skipped: int = 0        # векторы «file:…» полной пересборки: SHA-256 неизвестен (папка вне программы)
    missing_upload_files: int = 0        # вектор фото из браузера, файла которого нет
    index_model_unknown: bool = False    # индекс «непроверенный»: его векторы в кэш не переносятся


def _index_rows(index) -> list[tuple[str, str | None]]:
    if index is None:
        return []
    sources = getattr(index, "_sources", None) or [None] * len(index._ids)
    return list(zip(index._ids, sources, strict=True))


def backfill(registry: PhotoRegistry, products: Mapping[str, str], uploads_root: Path, *, index=None, folder_of, list_photos,
             upload_ref, apply: bool = True) -> BackfillResult:
    """Записи о фото, которые уже есть (файлы в data/catalog-uploads, фото импорта в индексе), и векторы из индекса известной
    модели — в кэш. Повтор безопасен; файлы не трогаются. products: {id: артикул} из базы. apply=False — только посчитать
    (файлы не читаются, база не меняется; vectors_cached — сколько векторов индекса есть у записанных фото без кэша)."""
    res = BackfillResult()
    known = registry.known_source_refs()
    for pid, sku in sorted(products.items(), key=lambda kv: kv[1]):
        folder = folder_of(sku)
        infos = []
        for f in list_photos(uploads_root, sku):
            ref = upload_ref(folder, f.name)
            if ref in known:
                continue
            if not apply:
                infos.append(PhotoInfo(sha256="", source_ref=ref))
                continue
            try:
                infos.append(upload_photo_info(uploads_root, ref))
            except Exception as exc:  # noqa: BLE001 — одно нечитаемое фото не останавливает остальные
                res.unreadable.append((ref, type(exc).__name__))
        if infos:
            if apply:
                registry.remember(pid, infos, None, None)
            res.uploads_registered += len(infos)
            known.update(i.source_ref for i in infos)
    rows = _index_rows(index)
    imports: dict[str, list[PhotoInfo]] = {}
    for pid, source in rows:
        if pid not in products or not source or source in known:
            continue
        if source.startswith("import:"):
            sha = source[len("import:"):]
            if len(sha) == 64:
                imports.setdefault(pid, []).append(PhotoInfo(sha256=sha, source_ref=source))
                known.add(source)
        elif source.startswith("file:"):
            res.file_sources_skipped += 1
        elif source.startswith("upload:"):
            res.missing_upload_files += 1
    for pid, infos in imports.items():
        if apply:
            registry.remember(pid, infos, None, None)
        res.import_registered += len(infos)
    signature = getattr(index, "model", None) if index is not None else None
    if rows and signature is None:
        res.index_model_unknown = True
    elif rows:
        res.vectors_cached = cache_index_vectors(registry, index, products, apply=apply)
    return res


def cache_index_vectors(registry: PhotoRegistry, index, products: Mapping[str, str], apply: bool = True) -> int:
    """Векторы индекса ИЗВЕСТНОЙ модели — в кэш для записанных фото (по источнику; первый вектор источника)."""
    from app.services.index_reconcile import _vectors_of
    signature = getattr(index, "model", None)
    version = model_version(signature)
    if version is None:
        return 0
    cached = set(registry.cached_vectors(version))
    by_source: dict[tuple[str, str], int] = {}
    for row, (pid, source) in enumerate(_index_rows(index)):
        if pid in products and source:
            by_source.setdefault((pid, source), row)
    todo = [(img.id, by_source[(img.product_id, img.source_ref)]) for img in registry.images(products)
            if img.id not in cached and img.source_ref and (img.product_id, img.source_ref) in by_source]
    if not todo or not apply:
        return len(todo)
    vectors = _vectors_of(index)
    return registry.store_vectors(((image_id, vectors[row]) for image_id, row in todo), signature)


# --------------------------------------------------------------------------- пересборка индекса из кэша


@dataclass
class RebuildItem:
    image_id: str
    product_id: str
    source: str
    storage_path: str | None
    vector: np.ndarray | None


@dataclass
class RebuildPlan:
    version: str
    items: list[RebuildItem]
    inactive: int = 0

    @property
    def cached(self) -> list[RebuildItem]:
        return [i for i in self.items if i.vector is not None]

    @property
    def missing(self) -> list[RebuildItem]:
        return [i for i in self.items if i.vector is None]


def plan_rebuild(registry: PhotoRegistry, products: Iterable[str], version: str) -> RebuildPlan:
    """Что войдёт в индекс: активные эталоны товаров из базы и их векторы версии version из кэша (None — вектора нет)."""
    cache = registry.cached_vectors(version)
    items, inactive = [], 0
    for img in registry.images(products):
        if img.role != REFERENCE:
            continue
        if not img.is_active:
            inactive += 1
            continue
        items.append(RebuildItem(img.id, img.product_id, img.source_ref or f"image:{img.id}", img.storage_path,
                                 cache.get(img.id)))
    return RebuildPlan(version, items, inactive)


@dataclass
class RebuildResult:
    index: object
    from_cache: int
    computed: int
    left_out: list[tuple[str, str, str]]          # (товар, источник, причина)


def build_index(plan: RebuildPlan, signature: dict, embedding_dim: int, *, registry: PhotoRegistry | None = None,
                embedder=None, media_root: Path | None = None, load_image: Callable | None = None,
                allow_missing: bool = False) -> RebuildResult:
    """Новый индекс модели signature из плана. Векторы без кэша считаются моделью (embedder + файл фото в media_root) и
    записываются в кэш; если модели нет — отказ (allow_missing=False) или фото не войдёт в индекс (allow_missing=True)."""
    from app.pipeline.embedder import model_signature
    from app.pipeline.matcher import open_catalog_index
    if model_version(signature) != plan.version:
        raise ValueError("паспорт модели не соответствует версии плана")
    if embedder is not None and model_version(model_signature(embedder)) != plan.version:
        raise ValueError("загруженная модель не та, для которой строится индекс")
    vectors: dict[str, np.ndarray] = {i.image_id: i.vector for i in plan.cached}
    left_out: list[tuple[str, str, str]] = []
    computed = 0
    missing = plan.missing
    from_base = getattr(embedder, "vectors_from_base", None)
    if from_base is not None and registry is not None:
        # эмбеддер с проекцией (app/pipeline/projection.py): вектор модели без проекции уже в кэше -> проекция, без пересчёта фото.
        # В кэш — для ВСЕХ фото с вектором модели, и выведенных из эталонов тоже: «Вернуть в эталоны» берёт вектор из кэша
        # модели индекса (app/services/index_prune.py)
        projected = from_base(registry, None)
        if projected:
            registry.store_vectors(list(projected.items()), signature)
            taken = {i.image_id: projected[i.image_id] for i in missing if i.image_id in projected}
            vectors.update(taken)
            computed += len(taken)
            missing = [i for i in missing if i.image_id not in taken]
    if missing and embedder is None and not allow_missing:
        raise RuntimeError(f"у {len(missing)} фото нет вектора этой модели в кэше; нужна модель (--compute-missing)")
    if missing and embedder is not None:
        if load_image is None:
            from app.pipeline.imageio import load_rgb as load_image
        for item in missing:
            if not item.storage_path or media_root is None:
                left_out.append((item.product_id, item.source, "файл фото вне программы"))
                continue
            try:
                image = load_image(Path(media_root) / item.storage_path)
            except Exception as exc:  # noqa: BLE001 — одно нечитаемое фото не останавливает пересборку
                left_out.append((item.product_id, item.source, f"не читается ({type(exc).__name__})"))
                continue
            vec = np.atleast_2d(embedder.embed([image]))[0]
            vectors[item.image_id] = vec
            computed += 1
            if registry is not None:
                registry.store_vectors([(item.image_id, vec)], signature)
    elif missing:
        left_out += [(i.product_id, i.source, "нет вектора в кэше") for i in missing]
    index = open_catalog_index(None, embedding_dim, signature)
    by_product: dict[str, list[RebuildItem]] = {}
    for item in plan.items:
        if item.image_id in vectors:
            by_product.setdefault(item.product_id, []).append(item)
    for pid, items in by_product.items():
        rows = np.stack([np.asarray(vectors[i.image_id], dtype=np.float32) for i in items])
        if rows.shape[1] != embedding_dim:
            raise ValueError(f"размерность векторов в кэше {rows.shape[1]}, а индекс — {embedding_dim}")
        index.add(pid, rows, [i.source for i in items])
    return RebuildResult(index, len(plan.cached), computed, left_out)
