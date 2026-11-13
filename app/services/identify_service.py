"""
«Узнать товар»: человек показывает камере ОДИН товар — система называет его. Чистая логика (без FastAPI/SQLAlchemy/моделей:
эмбеддер, матчер и поиск названий подаются снаружи), поэтому проверяется офлайн (tests/test_identify_service.py).
Реальная проводка — задача воркера identify_product_task (app/worker.py) и маршрут POST /api/v1/catalog/identify.

Решение «товар / похоже / не знаю» принимает ТА ЖЕ функция decide_match с теми же порогами из config.yaml, что и при разборе полки, —
отдельных «только для демо» порогов нет. Поэтому и недостатки те же: пока пороги не подобраны на реальных данных
(ml/evaluate_matching.py, docs/pilot_plan.md), граница «уверенно / похоже» ориентировочная.

Статусы ответа (для человека — простыми словами в интерфейсе):
  matched        — уверенно узнан: product заполнен;
  uncertain      — похож на product, но система не уверена: показываем и запасные варианты (candidates);
  unknown        — такого товара в каталоге нет (или кадр без товара);
  empty_catalog  — в каталоге вообще нет эталонов: сначала нужно добавить товары.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

import numpy as np

from app.core.schemas import DetectionStatus
from app.pipeline.matcher import decide_match

CANDIDATES_SHOWN = 3
DEEP_SEARCH_MAX_VECTORS = 2000  # предел углублённого поиска, когда лучшие места заняты векторами товаров, которых нет в базе


@dataclass(frozen=True)
class ProductInfo:
    id: str
    name: str
    brand: str = ""
    expected_price: float | None = None


Lookup = Callable[[list[str]], Mapping[str, ProductInfo]]


def _as_product(info: ProductInfo, score: float) -> dict:
    return {"id": info.id, "name": info.name, "brand": info.brand, "expected_price": info.expected_price, "score": round(float(score), 3)}


def _ranked_existing(vector: np.ndarray, matcher, lookup: Lookup) -> tuple[list[tuple[str, float]], dict, bool]:
    """Кандидаты, которые ЕСТЬ в базе (база — единственный источник истины), лучший первым; ответ поиска названий; пуст ли индекс.

    Векторы товаров, которых нет в базе (удалён; запись оборвалась в старой версии), пропускаются. Если они заняли лучшие места
    и настоящих товаров в выдаче меньше двух (для правила запаса нужен соперник), поиск углубляется — иначе товар, стоящий
    в индексе дальше, остался бы неузнанным из-за чужих векторов."""
    ranked = matcher.rank(vector)
    if not ranked:
        return [], {}, True
    infos = dict(lookup([product_id for product_id, _ in ranked]))
    existing = [(pid, score) for pid, score in ranked if pid in infos]
    total = _index_total(matcher)
    k = max(getattr(matcher, "candidates_k", 25), getattr(matcher, "top_k", 5))
    # глубже — только если «призраки» действительно заняли места (len(existing) < len(ranked)) и есть куда углубляться
    while len(existing) < 2 and len(existing) < len(ranked) and k < min(total, DEEP_SEARCH_MAX_VECTORS):
        k = min(k * 4, total, DEEP_SEARCH_MAX_VECTORS)
        ranked = matcher.rank(vector, k=k)
        missing = [pid for pid, _ in ranked if pid not in infos]
        if missing:
            infos.update(lookup(missing))
        existing = [(pid, score) for pid, score in ranked if pid in infos]
    return existing, infos, False


def _index_total(matcher) -> int:
    index = getattr(matcher, "index", None)
    ids = getattr(index, "_ids", None)
    return len(ids) if ids is not None else 0


def identify(image: np.ndarray, embedder, matcher, lookup: Lookup) -> dict:
    """Один кадр -> {"status", "product", "score", "candidates"} (см. docstring модуля). Товар в ответе — только из базы."""
    vector = embedder.embed([image])[0]
    ranked, infos, empty = _ranked_existing(vector, matcher, lookup)
    if empty:
        return {"status": "empty_catalog", "product": None, "score": 0.0, "candidates": []}
    if not ranked:
        return {"status": "unknown", "product": None, "score": 0.0, "candidates": []}
    decision = decide_match(ranked, matcher.match_threshold, matcher.unknown_threshold, matcher.margin_threshold)
    candidates = [_as_product(infos[product_id], score) for product_id, score in ranked[:CANDIDATES_SHOWN]]
    if decision.status == DetectionStatus.UNKNOWN:
        return {"status": "unknown", "product": None, "score": round(float(decision.score), 3), "candidates": candidates}
    status = "matched" if decision.status == DetectionStatus.MATCHED else "uncertain"
    return {"status": status, "product": _as_product(infos[decision.product_id], decision.score),
            "score": round(float(decision.score), 3), "candidates": candidates}


def add_reference_vectors(embedder, index, product_id: str, images: list[np.ndarray], index_path: str | None) -> int:
    """Эталонные фото существующего товара -> вектора в индекс (+ сохранение на диск). Возвращает число добавленных векторов.
    Индекс не умеет удалять векторы, поэтому «добавить фото» — единственная безопасная правка эталонов."""
    if not images:
        raise ValueError("Нет фото для добавления")
    vectors = np.atleast_2d(embedder.embed(images))
    index.add(product_id, vectors)
    if index_path:
        index.save(index_path)
    return int(len(vectors))
