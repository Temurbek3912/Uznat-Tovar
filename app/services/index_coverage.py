"""
Контроль полноты индекса и точечное восстановление (2026-11-07).

У каждого вектора индекса записан источник — какое фото его дало (matcher: `sources`, параллельно `ids`):
  «upload:<папка>/<файл>»  — фото из браузера (data/catalog-uploads/<папка>/<файл>, имя «NNN-<12 знаков SHA-256>.jpg»);
  «import:<SHA-256>»       — фото из папки импорта (scripts/import_catalog.py); копия файла — data/catalog-imported, путь и
                             SHA-256 — в записи о фото (product_images);
  «file:<папка>/<файл>»    — полная пересборка ml/build_catalog_embeddings.py;
  нет источника            — векторы версий до 2026-11-07 («старые»).
Индекс сам говорит, какие фото в нём есть: отдельный журнал uploads_manifest.json больше не источник истины (он ведётся для
совместимости и учитывается только для товаров со «старыми» векторами, у которых источника нет).

Что обнаруживается (analyze_coverage):
  - фото на диске без вектора (не проиндексировано или вектор потерян) — точечно, по каждому фото;
  - вектор, чьего фото нет на диске (удалено или перемещено после индексирования);
  - вектор фото из ЧУЖОЙ папки товара (фото «приписано» не тому товару);
  - повторное индексирование одного фото (один источник у нескольких векторов);
  - изменение фото после индексирования (содержимое не совпадает с SHA-256 в имени) — по запросу, это чтение всех фото;
  - товары без векторов и без фото (восстановить нечем — нужны новые фото);
  - «старые» векторы без источника (их нельзя проверить поштучно).
Фото импорта (2026-11-21) проверяются по записям о фото (import_photos, product_images): есть ли запись этого товара, есть ли
копия файла в папке данных, не изменилось ли содержимое (--verify-photos: SHA-256 файла с записью), у каждого ли активного
эталона импорта есть вектор. Без записей (import_photos=None) векторы импорта только считаются — как раньше.
Восстановление:
  - недостающие векторы фото из браузера — recover_pending (нужна модель: задача воркера при каждом запуске и
    scripts/reindex_uploads.py); повтор безопасен — «есть ли вектор» каждый раз решает сам индекс;
  - повторы одного фото убирает scripts/reconcile_index.py --apply (без модели; векторы те же — теряется только лишнее).
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Iterable, Mapping

import numpy as np

UPLOAD = "upload:"
IMPORT = "import:"
FILE = "file:"


def _upload_key(source: str) -> str | None:
    return source[len(UPLOAD):] if isinstance(source, str) and source.startswith(UPLOAD) else None


@dataclass
class IndexedState:
    """Снимок «что в индексе»: какие источники есть и у каких товаров есть векторы без источника."""
    sources: Counter
    legacy_products: set[str]
    vectors_by_product: Counter

    @classmethod
    def of(cls, index) -> "IndexedState":
        sources = getattr(index, "_sources", None) or [None] * len(index._ids)
        return cls(Counter(s for s in sources if s), {pid for pid, s in zip(index._ids, sources, strict=True) if s is None},
                   Counter(index._ids))

    def has(self, product_id: str | None, ref: str, manifest_has: Callable[[str], bool] = lambda r: False) -> bool:
        """Есть ли вектор этого фото. Для товара со «старыми» векторами (без источника) верим журналу."""
        if self.sources.get(ref):
            return True
        legacy = product_id in self.legacy_products if product_id is not None else bool(self.legacy_products)
        return legacy and manifest_has(ref)


def not_indexed_filter(index, manifest_keys: set[str] | None = None, product_id: str | None = None) -> Callable[[list[str]], list[str]]:
    """Для app/services/catalog_add: какие из ссылок ещё не превращены в векторы — по самому индексу."""
    state = IndexedState.of(index)
    keys = manifest_keys or set()
    return lambda refs: [r for r in refs if not state.has(product_id, r, lambda x: (_upload_key(x) or x) in keys)]


def _sha12_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ImportPhoto:
    """Запись о фото импорта (product_images с источником «import:<SHA-256>»)."""
    product_id: str
    source_ref: str
    storage_path: str | None      # копия файла внутри папки данных, через «/»; None — копии нет (импорт до копирования фото)
    sha256: str
    reference: bool               # активный эталон — участвует в распознавании, вектор нужен


def _file_inside(media_root: Path, rel: str) -> Path | None:
    """Файл копии внутри папки данных; путь из базы наружу (абсолютный, «..», ссылка вне папки) — None, без чтения."""
    r = Path(rel)
    if r.is_absolute() or r.drive or ".." in r.parts:
        return None
    root = Path(media_root).resolve()
    try:
        real = (root / r).resolve()
    except OSError:
        return None
    return real if real.is_relative_to(root) and real.is_file() else None


@dataclass
class CoverageReport:
    photos_not_indexed: list[tuple[str, str, str]] = field(default_factory=list)       # (id, артикул, ссылка)
    vectors_without_photo: list[tuple[str, str]] = field(default_factory=list)        # (id, источник)
    foreign_photo_vectors: list[tuple[str, str]] = field(default_factory=list)        # (id, источник из чужой папки)
    duplicate_sources: dict[str, int] = field(default_factory=dict)                   # источник -> число векторов
    changed_photos: list[tuple[str, str]] = field(default_factory=list)               # (id, ссылка) — если проверялось
    products_without_vectors: list[tuple[str, str]] = field(default_factory=list)
    products_without_vectors_or_photos: list[tuple[str, str]] = field(default_factory=list)
    legacy_vectors: dict[str, int] = field(default_factory=dict)                      # id -> число векторов без источника
    import_vectors: int = 0                                                           # векторы фото импорта (всего)
    content_checked: bool = False
    imports_checked: bool = False                                                     # фото импорта сверены с записями о фото
    import_vectors_without_record: list[tuple[str, str]] = field(default_factory=list)  # (id, источник) — у товара нет записи
    import_vectors_without_copy: int = 0                                              # запись без копии файла — не проверяются
    import_photos_not_indexed: list[tuple[str, str]] = field(default_factory=list)    # (id, источник) — эталон без вектора

    @property
    def complete(self) -> bool:
        return not (self.photos_not_indexed or self.vectors_without_photo or self.foreign_photo_vectors or self.duplicate_sources
                    or self.changed_photos or self.products_without_vectors or self.import_vectors_without_record
                    or self.import_photos_not_indexed)


def analyze_coverage(index, products: Mapping[str, str], uploads_root: Path, *, folder_of, list_photos,
                     upload_ref, manifest_keys: set[str] | None = None, verify_content: bool = False,
                     skip: Collection[tuple[str, str]] = frozenset(), import_photos: Iterable[ImportPhoto] | None = None,
                     media_root: Path | None = None) -> CoverageReport:
    """products: {id: артикул} из базы. folder_of(артикул) -> папка; list_photos(root, артикул) -> файлы фото;
    upload_ref(папка, файл) -> ссылка. Векторы товаров, которых нет в базе, — дело scripts/reconcile_index.py (здесь не считаются).
    skip — (товар, ссылка) фото, которые не участвуют в распознавании (PhotoRegistry.excluded_sources): отсутствие их
    вектора не ошибка (вектор выведенного фото убирает app/services/index_prune.py). import_photos — записи о фото импорта,
    media_root — папка данных (их копии): с ними проверяются и векторы фото импорта (иначе только считаются)."""
    state = IndexedState.of(index)
    imports = None if import_photos is None else list(import_photos)
    if imports is not None and media_root is None:
        raise ValueError("для проверки фото импорта нужна папка данных (media_root)")
    by_source: dict[str, list[ImportPhoto]] = {}
    for rec in imports or ():
        by_source.setdefault(rec.source_ref, []).append(rec)
    checked: set[tuple[str, str]] = set()
    keys = manifest_keys or set()
    rep = CoverageReport(content_checked=verify_content, imports_checked=imports is not None)
    sources = getattr(index, "_sources", None) or [None] * len(index._ids)
    folder_by_product = {pid: folder_of(sku) for pid, sku in products.items()}
    for pid, source in zip(index._ids, sources, strict=True):
        if pid not in products:
            continue
        if source is None:
            rep.legacy_vectors[pid] = rep.legacy_vectors.get(pid, 0) + 1
        elif source.startswith(IMPORT):
            rep.import_vectors += 1
            if imports is None or (pid, source) in skip or (pid, source) in checked:   # выведенные — уберёт index_prune
                continue
            checked.add((pid, source))
            recs = by_source.get(source, [])
            own = [r for r in recs if r.product_id == pid]
            if not own:
                (rep.foreign_photo_vectors if recs else rep.import_vectors_without_record).append((pid, source))
                continue
            rec = next((r for r in own if r.storage_path), own[0])
            if not rec.storage_path:
                rep.import_vectors_without_copy += 1
            elif (path := _file_inside(media_root, rec.storage_path)) is None:
                rep.vectors_without_photo.append((pid, source))
            elif verify_content and _sha256_of(path) != (rec.sha256 or "").lower():
                rep.changed_photos.append((pid, source))
        elif (key := _upload_key(source)) is not None:
            folder, _, name = key.partition("/")
            if folder != folder_by_product[pid]:
                rep.foreign_photo_vectors.append((pid, source))
            elif not (Path(uploads_root) / folder / name).is_file():
                rep.vectors_without_photo.append((pid, source))
    rep.duplicate_sources = {s: n for s, n in state.sources.items() if n > 1}
    indexed_pairs = set(zip(index._ids, sources, strict=True))
    import_refs_by_product: Counter = Counter()
    for rec in imports or ():
        pair = (rec.product_id, rec.source_ref)
        if not rec.reference or rec.product_id not in products or pair in skip:
            continue
        import_refs_by_product[rec.product_id] += 1
        if pair not in indexed_pairs and pair not in rep.import_photos_not_indexed:
            rep.import_photos_not_indexed.append(pair)
    for pid, sku in sorted(products.items(), key=lambda kv: kv[1]):
        files = list_photos(uploads_root, sku)
        for f in files:
            ref = upload_ref(folder_by_product[pid], f.name)
            if not state.has(pid, ref, lambda r: (_upload_key(r) or r) in keys):
                if (pid, ref) not in skip:
                    rep.photos_not_indexed.append((pid, sku, ref))
            elif verify_content and state.sources.get(ref):
                expected = f.name.split("-", 1)[1].split(".", 1)[0] if "-" in f.name else ""
                if _sha12_of(f) != expected:
                    rep.changed_photos.append((pid, ref))
        if state.vectors_by_product.get(pid, 0) == 0:
            rep.products_without_vectors.append((pid, sku))
            if not files and not import_refs_by_product.get(pid):
                rep.products_without_vectors_or_photos.append((pid, sku))
    return rep


@dataclass
class RecoveryResult:
    added: list[tuple[str, str]] = field(default_factory=list)           # (id, ссылка)
    failed: list[tuple[str, str, str]] = field(default_factory=list)     # (id, ссылка, причина) — фото не читается
    remaining: int = 0                                                   # ещё не обработано (сработал предел за один запуск)

    @property
    def progressed(self) -> bool:
        return bool(self.added)


def recover_pending(index, products: Mapping[str, str], uploads_root: Path, *, embedder, index_path: str | None,
                    folder_of, list_photos, upload_ref, load_image, manifest_keys: set[str] | None = None,
                    note_indexed: Callable[[list[str]], None] = lambda refs: None, limit: int = 200,
                    remember: Callable[[str, list[str], np.ndarray], None] | None = None,
                    skip: Collection[tuple[str, str]] = frozenset()) -> RecoveryResult:
    """Векторы для фото из браузера, которых нет в индексе, — по одному товару: векторы -> индекс -> сохранение -> журнал.
    Товары берутся только из базы. Повтор безопасен: что уже в индексе, решает сам индекс. Непригодное фото — в failed,
    остальные продолжаются. Не больше limit фото за запуск (remaining — сколько осталось). skip — (товар, ссылка) фото, выведенных
    из эталонов или витринных (PhotoRegistry.excluded_sources): их векторы не добавляются — иначе вектор, убранный
    app/services/index_prune.py, вернулся бы при следующем запуске."""
    res = RecoveryResult()
    keys = manifest_keys or set()
    budget = limit
    state = IndexedState.of(index)
    for pid, sku in sorted(products.items(), key=lambda kv: kv[1]):
        folder = folder_of(sku)
        todo = [(f, upload_ref(folder, f.name)) for f in list_photos(uploads_root, sku)]
        todo = [(f, ref) for f, ref in todo
                if (pid, ref) not in skip and not state.has(pid, ref, lambda r: (_upload_key(r) or r) in keys)]
        if not todo:
            continue
        if budget <= 0:
            res.remaining += len(todo)
            continue
        batch, res.remaining = todo[:budget], res.remaining + max(0, len(todo) - budget)
        images, refs = [], []
        for f, ref in batch:
            try:
                images.append(load_image(f))
                refs.append(ref)
            except Exception as exc:  # noqa: BLE001 — одно непригодное фото не останавливает остальные
                res.failed.append((pid, ref, f"{type(exc).__name__}: {exc}"))
        budget -= len(batch)
        if not images:
            continue
        vectors = np.atleast_2d(embedder.embed(images))
        if remember is not None:                        # записи о фото и векторы — в базу до индекса (product_images)
            remember(pid, refs, vectors)
        index.add(pid, vectors, refs)
        if index_path:
            index.save(index_path)
        note_indexed(refs)
        res.added += [(pid, r) for r in refs]
        state = IndexedState.of(index)
    return res


def describe(rep: CoverageReport) -> Iterable[str]:
    yield f"Фото без вектора: {len(rep.photos_not_indexed)}"
    yield f"Векторы, чьих фото нет на диске: {len(rep.vectors_without_photo)}"
    yield f"Векторы фото из папки другого товара: {len(rep.foreign_photo_vectors)}"
    yield f"Фото, проиндексированные повторно: {len(rep.duplicate_sources)}"
    yield ("Фото, изменённые после индексирования: " + str(len(rep.changed_photos))) if rep.content_checked else \
          "Изменение фото после индексирования: не проверялось (--verify-photos)"
    yield f"Товары без векторов: {len(rep.products_without_vectors)}; из них без фото (восстановить нечем): " \
          f"{len(rep.products_without_vectors_or_photos)}"
    yield f"Векторы без источника (версии до 2026-11-07, поштучно не проверяются): {sum(rep.legacy_vectors.values())}"
    if rep.imports_checked:
        yield f"Векторы фото импорта: {rep.import_vectors}; без записи о фото у товара: {len(rep.import_vectors_without_record)}; " \
              f"запись без копии файла (не проверяются): {rep.import_vectors_without_copy}"
        yield f"Фото импорта без вектора: {len(rep.import_photos_not_indexed)}"
    else:
        yield f"Векторы фото импорта (записи о фото не загружены — не проверялись): {rep.import_vectors}"
