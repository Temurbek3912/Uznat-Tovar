"""
Добавление товара в каталог: база данных — единственный источник истины о существующих товарах (2026-11-07).

Порядок (раньше было наоборот: векторы в индекс и на диск, затем commit базы):
  1. фото -> векторы (до любых записей: непригодное фото или сбой модели не оставляют ничего);
  2. товар в базе — commit. Если товар с этим артикулом уже есть (повтор задачи после сбоя, одновременный запрос с тем же
     названием), используется он: операция идемпотентна;
  3. записи о фото и их векторы — в базу (product_images, кэш image_embeddings; callback remember, с 2026-11-14);
  4. только затем векторы — в индекс, сохранение индекса, отметка фото в журнале проиндексированных.

Следствия:
  - в индексе не появляются векторы товара, которого нет в базе (откат базы больше не оставляет «призраков» на диске);
  - сбой после шага 2 оставляет товар без векторов: он находится по штрихкоду и виден в каталоге, а векторы добавит повтор
    задачи или `python -m scripts.reconcile_index` / `scripts.reindex_uploads` (фото уже лежат на диске);
  - повтор не добавляет векторы фото, уже отмеченных в журнале.
Модуль без SQLAlchemy, Celery и моделей: база подаётся через ProductStore (реализация — app/services/catalog_service.py),
поэтому логика проверяется тестами без них (tests/test_catalog_add.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence

import numpy as np


class DuplicateSku(Exception):
    """Товар с таким артикулом уже есть в базе (нарушение уникальности при commit)."""


class ProductStore(Protocol):
    def find_id_by_sku(self, sku: str) -> str | None: ...

    def create(self, product_in) -> str:
        """Создаёт товар и фиксирует (commit). Артикул занят — DuplicateSku (сессия после этого пригодна к работе)."""
        ...


@dataclass
class AddOutcome:
    product_id: str
    created: bool                          # False — товар уже был (повтор или одновременный запрос с тем же артикулом)
    vectors_added: int
    skipped_refs: list[str] = field(default_factory=list)   # фото, уже бывшие в индексе


def ensure_product(product_in, store: ProductStore) -> tuple[str, bool]:
    """Товар в базе: найти по артикулу или создать. Гонка двух созданий решается уникальностью артикула в базе."""
    existing = store.find_id_by_sku(product_in.sku)
    if existing is not None:
        return existing, False
    try:
        return store.create(product_in), True
    except DuplicateSku:
        winner = store.find_id_by_sku(product_in.sku)
        if winner is None:                         # уникальность нарушена не артикулом — это не наш случай, наверх
            raise
        return winner, False


def _select(refs: Sequence[str], not_indexed: Callable[[list[str]], list[str]]) -> tuple[list[int], list[str]]:
    todo = set(not_indexed(list(refs)))
    return [i for i, ref in enumerate(refs) if ref in todo], [ref for ref in refs if ref not in todo]


Remember = Callable[[str, list[str], np.ndarray], None]      # (товар, ссылки фото, их векторы) -> записи в базе


def _store_vectors(product_id: str, refs: Sequence[str], picked: list[int], vectors, *, index, index_path: str | None,
                   note_indexed: Callable[[list[str]], None], remember: Remember | None = None) -> int:
    if not picked:
        return 0
    if remember is not None:                                        # база — до индекса: индекс восстановим из неё
        remember(product_id, [refs[i] for i in picked], vectors)
    index.add(product_id, vectors, [refs[i] for i in picked])       # источник каждого вектора — его фото
    if index_path:
        index.save(index_path)
    note_indexed([refs[i] for i in picked])
    return int(len(vectors))


def index_photos(product_id: str, refs: Sequence[str], images: Sequence[np.ndarray], *, embedder, index, index_path: str | None,
                 not_indexed: Callable[[list[str]], list[str]], note_indexed: Callable[[list[str]], None],
                 remember: Remember | None = None) -> tuple[int, list[str]]:
    """Векторы фото УЖЕ существующего в базе товара -> индекс -> сохранение -> журнал. Только фото, которых нет в журнале."""
    if len(refs) != len(images):
        raise ValueError("Количество фото не совпадает со списком ссылок")
    picked, skipped = _select(refs, not_indexed)
    vectors = np.atleast_2d(embedder.embed([images[i] for i in picked])) if picked else None
    return _store_vectors(product_id, refs, picked, vectors, index=index, index_path=index_path, note_indexed=note_indexed,
                          remember=remember), skipped


def add_product(product_in, images: Sequence[np.ndarray], *, store: ProductStore, embedder, index, index_path: str | None,
                not_indexed: Callable[[list[str]], list[str]] | None = None,
                note_indexed: Callable[[list[str]], None] | None = None, remember: Remember | None = None) -> AddOutcome:
    """Новый товар с эталонными фото (см. порядок в описании модуля)."""
    refs = list(product_in.reference_image_urls)
    if len(images) != len(refs):
        raise ValueError("Количество загруженных фото не совпадает со списком URL")
    not_indexed = not_indexed or (lambda r: list(r))
    note_indexed = note_indexed or (lambda r: None)
    picked, skipped = _select(refs, not_indexed)
    vectors = np.atleast_2d(embedder.embed([images[i] for i in picked])) if picked else None   # 1) до записи в базу
    product_id, created = ensure_product(product_in, store)                                       # 2) товар — commit
    added = _store_vectors(product_id, refs, picked, vectors, index=index, index_path=index_path,  # 3) векторы
                           note_indexed=note_indexed, remember=remember)
    return AddOutcome(product_id, created, added, skipped)
