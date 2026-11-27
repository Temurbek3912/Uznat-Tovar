"""
Категория нового товара сама (задание владельца 2026-11-27): владелец добавил товар и не выбрал категорию — программа ставит её
по трём подсказкам вместо «Без категории».

1. Название — правила по словам (open_product_details.category_from_name): «Майонез …» -> «Соусы и специи».
2. Соседи по фото — похожие товары каталога в векторном индексе (тот же поиск, что «Узнать товар»): если ближайшие товары почти
   все «Сладости», новый — тоже. Голос каждого соседа = его сходство с фото; сосед без категории не голосует.
3. Вид по фото — малая модель «картинка + текст» (app/pipeline/kind_classifier.py), вид -> категория (app/services/categories.py).

Подсказки складываются с весами: у каждой категории — сумма «название назвало её» × 1, «доля голосов соседей» × 1 и «уверенность
малой модели в этом виде» × 1,5; побеждает наибольшая сумма. Если её доля от всех весов ниже 0,2 — категория не ставится (лучше
«Без категории», чем неверная). Веса и порог подобраны замером на каталоге владельца (ml/benchmark_categories.py, отчёт
docs/benchmark/categories_report.md): верно 91 % названных против 84 % у «первое сработавшее правило».
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from app.services.categories import category_for_kind, is_known

NEIGHBORS = 5            # сколько ближайших товаров голосуют
MIN_SIMILARITY = 0.7     # сосед дальше этого — не похож, не голосует
W_NAME, W_NEIGHBORS, W_KIND = 1.0, 1.0, 1.5
MIN_SHARE = 0.2          # доля суммы победителя от всех весов; ниже — не уверены


@dataclass(frozen=True)
class CategoryGuess:
    category: str
    source: str          # главная подсказка: "name" | "neighbors" | "kind_model"
    confidence: float    # 0..1: доля суммы победителя от всех весов


def _votes(neighbors: Iterable[tuple[str, float]], top: int = NEIGHBORS, min_similarity: float = MIN_SIMILARITY
           ) -> dict[str, float]:
    """Голоса первых `top` соседей с категорией и сходством не ниже `min_similarity`; вес голоса — сходство."""
    votes: dict[str, float] = {}
    counted = 0
    for category, similarity in neighbors:
        if counted >= top:
            break
        if not is_known(category) or similarity < min_similarity:
            continue
        votes[category] = votes.get(category, 0.0) + float(similarity)
        counted += 1
    return votes


def neighbor_vote(neighbors: Iterable[tuple[str, float]], *, top: int = NEIGHBORS, min_similarity: float = MIN_SIMILARITY
                  ) -> tuple[str, float] | None:
    """Соседи (категория, сходство), по одному на товар, от ближнего к дальнему -> (категория-победитель, доля голосов) или None."""
    votes = _votes(neighbors, top, min_similarity)
    if not votes:
        return None
    winner = max(votes, key=votes.get)
    return winner, votes[winner] / sum(votes.values())


def guess_category(name: str | None, neighbors: Sequence[tuple[str, float]] = (), kind_probs: dict[str, float] | None = None,
                   *, min_share: float = MIN_SHARE) -> CategoryGuess | None:
    """Название + соседи по фото + уверенность малой модели по видам -> категория или None (не уверены).

    kind_probs — {вид: доля уверенности} (KindScore.probs); None — модели нет, считается без неё (и её вес не учитывается)."""
    from app.services.open_product_details import category_from_name  # noqa: PLC0415 — большой модуль правил

    parts: dict[str, dict[str, float]] = {}

    def add(category: str, source: str, value: float) -> None:
        parts.setdefault(category, {})
        parts[category][source] = parts[category].get(source, 0.0) + value

    by_name = category_from_name(name or "")
    if by_name:
        add(by_name, "name", W_NAME)
    votes = _votes(neighbors)
    total = sum(votes.values())
    for category, weight in votes.items():
        add(category, "neighbors", W_NEIGHBORS * weight / total)
    for kind, prob in (kind_probs or {}).items():
        category = category_for_kind(kind)
        if category:
            add(category, "kind_model", W_KIND * float(prob))
    if not parts:
        return None
    weights = W_NAME + W_NEIGHBORS + (W_KIND if kind_probs else 0.0)
    winner = max(parts, key=lambda c: sum(parts[c].values()))
    share = sum(parts[winner].values()) / weights
    if share < min_share:
        return None
    source = max(parts[winner], key=parts[winner].get)
    return CategoryGuess(winner, source, round(share, 3))


def product_neighbors(hits: Iterable[tuple[str, float]], categories: dict[str, str], exclude: str | None = None
                      ) -> list[tuple[str, float]]:
    """Ответ индекса (product_id, сходство) по векторам -> соседи (категория, лучшее сходство) по одному на товар, по убыванию.

    `categories` — product_id -> категория (архивные не передаются); `exclude` — сам товар (при замере и повторе задачи)."""
    best: dict[str, float] = {}
    for product_id, score in hits:
        if product_id == exclude or product_id not in categories:
            continue
        if score > best.get(product_id, -1.0):
            best[product_id] = float(score)
    ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)
    return [(categories[pid], score) for pid, score in ranked]


SEARCH_K = 40            # векторов из индекса на каждое фото (потом — по одному на товар)


def neighbors_in_index(index, product_id: str, categories: dict[str, str]) -> list[tuple[str, float]]:
    """Соседи товара по его же векторам в индексе (сам товар не считается)."""
    vectors, _ = index.product_vectors(product_id)
    hits: list[tuple[str, float]] = []
    for vector in vectors:
        hits.extend(index.search(vector, SEARCH_K))
    return product_neighbors(hits, categories, exclude=product_id)


def kind_probs_for(image, settings) -> dict[str, float] | None:
    """Первое фото (RGB-массив) -> уверенность малой модели по видам; модели нет (выключена, не загрузилась) -> None."""
    if image is None or not getattr(settings, "naming_kind_model", ""):
        return None
    from PIL import Image  # noqa: PLC0415

    from app.pipeline.kind_classifier import get_classifier  # noqa: PLC0415 — torch только при включённой модели
    model = get_classifier(settings)
    if model is None:
        return None
    return model.classify_image(Image.fromarray(image)).probs


def guess_for_new_product(name: str, product_id: str, index, categories: dict[str, str], image=None, settings=None
                          ) -> CategoryGuess | None:
    """Новый товар уже в индексе -> категория по названию, соседям и малой модели (или None)."""
    neighbors = neighbors_in_index(index, product_id, categories) if index is not None else []
    probs = kind_probs_for(image, settings) if settings is not None else None
    return guess_category(name, neighbors, probs)
