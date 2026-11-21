"""
Векторы выведенных из эталонов фото — убрать из файла индекса, вернувшиеся в эталоны — вернуть (2026-11-21).

С 2026-11-14 фото, выведенное из эталонов на странице «Товары», исключается из распознавания по записи в базе
(ProductMatcher.excluded): сразу и обратимо, но его вектор оставался в файле индекса навсегда — до полной пересборки
(scripts/rebuild_index.py --apply). Здесь — плановая уборка, которую делает задача воркера recover_index:

  - УБИРАЕТСЯ вектор (товар, источник), если фото с этим источником выведено из эталонов (is_active = false) дольше
    PRUNE_AFTER_DAYS дней, у того же товара нет активного эталона с тем же источником, и вектор этого фото лежит в кэше
    базы (image_embeddings) для модели индекса. Последнее условие делает уборку обратимой без модели: «Вернуть в эталоны»
    возвращает вектор из кэша байт в байт. Нет вектора в кэше — вектор остаётся в индексе (исключён по записи, как раньше).
  - ВОЗВРАЩАЕТСЯ вектор активного эталона, которого нет в индексе, если он есть в кэше для модели индекса (фото вернули в
    эталоны после уборки; индекс восстановлен из старой копии).
  - Индекс без паспорта модели (построен до 2026-11-14) не трогается: нельзя доказать, что кэш — той же модели.
  - Векторы без источника (индексы до 07.11) не трогаются: какому фото они принадлежат, неизвестно. Товару с такими векторами
    векторы из кэша не возвращаются (фото, возможно, уже есть в индексе под вектором без источника).
  - Витринные фото (роль display) по-прежнему исключаются только по записи: у смены роли нет времени, а «сделать эталоном»
    должно действовать сразу.

Почему с задержкой, а не сразу при нажатии: в течение PRUNE_AFTER_DAYS «Вернуть» действует со следующего кадра, как раньше;
страница «Товары» работает в другом процессе (admin-api), у которого нет индекса; изменение файла индекса — только в
воркере, под блокировкой индекса и со сверкой поколения (NumpyCatalogIndex.save / FaissCatalogIndex.save: если файл
записал другой процесс — IndexChangedError, ничего не записано).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

import numpy as np

from app.pipeline import matcher as M
from app.pipeline.embedder import model_version

PRUNE_AFTER_DAYS = 7
_COPY_ATTRS = ("_disk_gen", "_disk_probe", "_disk_sha", "model", "namespace", "_expected")


@dataclass
class PrunePlan:
    remove_rows: list[int] = field(default_factory=list)                       # строки индекса, которые убрать
    removed: list[tuple[str, str]] = field(default_factory=list)               # (товар, источник) убираемых векторов
    restore: list[tuple[str, str, np.ndarray]] = field(default_factory=list)   # (товар, источник, вектор из кэша)
    kept_without_cache: int = 0       # выведены давно, но вектора нет в кэше — остаются в индексе (исключены по записи)
    skipped: str | None = None        # почему уборка не выполняется вовсе

    @property
    def changes(self) -> bool:
        return bool(self.remove_rows or self.restore)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)      # как в product_images: время UTC без пояса


def plan_prune(index, images: Iterable, cached: Mapping[str, np.ndarray], products: Mapping[str, str] | set[str], *,
               now: datetime | None = None, after_days: float = PRUNE_AFTER_DAYS) -> PrunePlan:
    """images — записи о фото (product_images: id, product_id, source_ref, role, is_active, deactivated_at); cached —
    {id фото: вектор} кэша для модели ЭТОГО индекса (PhotoRegistry.cached_vectors(model_version(index.model)));
    products — товары базы (векторы чужих id — дело scripts/reconcile_index.py, здесь не трогаются)."""
    plan = PrunePlan()
    if model_version(getattr(index, "model", None)) is None:
        plan.skipped = "у индекса не записан паспорт модели (индекс до 2026-11-14) — векторы не убираются и не возвращаются"
        return plan
    cutoff = (now or _utcnow()) - timedelta(days=after_days)
    images = [img for img in images if img.source_ref and img.product_id in products]
    active = {(img.product_id, img.source_ref) for img in images if img.role == "reference" and img.is_active}
    stale: dict[tuple[str, str], bool] = {}                     # (товар, источник) -> есть вектор в кэше
    for img in images:
        key = (img.product_id, img.source_ref)
        if img.is_active or key in active or img.deactivated_at is None or img.deactivated_at > cutoff:
            continue
        stale[key] = stale.get(key, False) or img.id in cached
    sources = getattr(index, "_sources", None) or [None] * len(index._ids)
    present = set()
    for row, (pid, source) in enumerate(zip(index._ids, sources, strict=True)):
        if source is None:
            continue
        present.add((pid, source))
        if (pid, source) in stale:
            if stale[(pid, source)]:
                plan.remove_rows.append(row)
                plan.removed.append((pid, source))
            else:
                plan.kept_without_cache += 1
    legacy = {pid for pid, source in zip(index._ids, sources, strict=True) if source is None}
    dim = index.dimension
    seen = set()
    for img in images:
        key = (img.product_id, img.source_ref)
        if key not in active or key in present or key in seen or img.id not in cached:
            continue
        if img.product_id in legacy:                            # у товара векторы без источника: это фото, может быть, уже там
            continue
        if not (img.role == "reference" and img.is_active):
            continue
        vec = np.asarray(cached[img.id], dtype=np.float32).ravel()
        if dim is not None and vec.size != dim:
            continue                                            # кэш другой размерности — не наш вектор
        seen.add(key)
        plan.restore.append((img.product_id, img.source_ref, vec))
    return plan


def apply_plan(index, plan: PrunePlan):
    """Новый индекс того же типа: прежние векторы байт в байт и в прежнем порядке без remove_rows, в конце — restore.
    Поколение файла переносится — сохранение (index.save) откажет, если файл за это время записал другой процесс."""
    from app.services.index_reconcile import _vectors_of
    drop = set(plan.remove_rows)
    vectors = _vectors_of(index)
    fresh = M.FaissCatalogIndex(embedding_dim=index.dimension) if isinstance(index, M.FaissCatalogIndex) else M.NumpyCatalogIndex()
    sources = getattr(index, "_sources", None) or [None] * len(index._ids)
    for row, pid in enumerate(index._ids):
        if row not in drop:
            fresh.add(pid, np.asarray(vectors[row], dtype=np.float32)[None, :], [sources[row]])
    for pid, source, vec in plan.restore:
        fresh.add(pid, vec[None, :], [source])
    for attr in _COPY_ATTRS:
        if attr in vars(index):
            setattr(fresh, attr, getattr(index, attr))
    return fresh


def prune_index(index, index_path: str | None, registry, products: Mapping[str, str], *, now: datetime | None = None,
                after_days: float = PRUNE_AFTER_DAYS):
    """План по базе и индексу; есть изменения — новый индекс сохраняется (блокировка + сверка поколения внутри save).
    Возвращает (индекс, которым теперь пользоваться, план). При ошибке сохранения — исключение, прежний индекс не тронут."""
    version = model_version(getattr(index, "model", None))
    cached = registry.cached_vectors(version) if version is not None else {}
    plan = plan_prune(index, registry.images(products), cached, products, now=now, after_days=after_days)
    if not plan.changes:
        return index, plan
    fresh = apply_plan(index, plan)
    if index_path:
        fresh.save(index_path)
    return fresh, plan
