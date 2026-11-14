"""
Сравнение моделей распознавания товара ПО ВИДУ на одиночных фото (benchmark; документ 07, §5 и §6).

    python -m ml.benchmark_identify synthetic --out data/benchmark/synthetic
    python -m ml.benchmark_identify list
    python -m ml.benchmark_identify photos --photos D:/набор --out data/benchmark/run1

Два режима.

* ``synthetic`` — проверка САМОГО ИНСТРУМЕНТА на искусственных векторах (без моделей и без фото). Цифры этого отчёта ничего не
  говорят о качестве моделей: они только показывают, что метрики, интервалы и разделение по съёмкам считаются верно.
* ``photos`` — настоящий набор владельца. Папки: ``<товар>/<съёмка>/*.jpg``. Товар, которого НЕТ в каталоге (проверка отказа),
  называется с приставкой ``чужой_`` (или ``unknown_``). Первая по имени съёмка каждого товара — эталоны (как фото в каталоге),
  остальные — запросы покупателя. Ни одна съёмка не бывает одновременно эталоном и запросом: соседние кадры одной серии почти
  одинаковы, и случайное разделение завысило бы точность (§5.1). Съёмка, указанная в ``--tune-session``, служит для подбора
  порогов, а итог считается только на остальных (контрольных) съёмках.

Что считается (§5.2): top-1 и top-5 по известным товарам; доля уверенных ответов (coverage); доля ошибок среди уверенных ответов
(selective risk — главная цифра, цель ≤ 1 %); правильный отказ и ложное принятие для товаров «не из каталога». Для каждой цифры —
95-процентный интервал bootstrap ПО ТОВАРАМ (все фото товара берутся или не берутся вместе: фото одного товара не независимы).
Когда ошибок ноль, интервал bootstrap схлопывается в [0; 0] — это нечестно, поэтому верхняя граница не меньше точной
«правила трёх» (1 − 0,05^(1/n) ≈ 3/n).

Решение «уверен / не уверен / неизвестно» принимает ``decide_match`` — та же функция, что в боевом матчере, а кандидаты дают
``NumpyCatalogIndex`` и ``rank_products`` из приложения. Поэтому цифры соответствуют поведению системы.

Конфигурации в режиме ``photos`` (§5.3, §6.1): DINOv2-base целым кадром (как сейчас), с центральным кропом, с дополнением до
квадрата; DINOv2-small; SigLIP 2 (только визуальная часть); кроп по детектору YOLO — если есть веса. Модель, которой нет в среде
(не установлены библиотеки или не скачаны веса), пропускается с пометкой в отчёте. Скачивание весов — только с ``--allow-download``.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

from app.core.schemas import DetectionStatus
from app.pipeline.matcher import NumpyCatalogIndex, decide_match, rank_products

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
UNKNOWN_PREFIXES = ("чужой", "unknown", "неизвестн", "нет_в_каталоге")

DEFAULT_MATCH = 0.72                     # configs/config.yaml, models.matcher
DEFAULT_UNKNOWN = 0.45
DEFAULT_MARGIN = 0.0
TARGET_RISK = 0.01                       # ошибок среди уверенных ответов не больше 1 % (§5.2)
CONFIDENT_FOR_1PCT = 300                 # столько уверенных ответов без ошибки нужно, чтобы утверждать «≤ 1 %»
SEARCH_K = 64                            # векторов из индекса; потом группировка по товару (как в боевом поиске)
MATCH_GRID = tuple(round(0.30 + 0.01 * i, 2) for i in range(66))          # 0.30 .. 0.95
MARGIN_GRID = (0.0, 0.02, 0.04, 0.06, 0.08, 0.10)

GALLERY, TUNE, TEST = "эталон", "подбор", "контроль"


# ============================================================================ данные

@dataclass(frozen=True)
class Item:
    """Одно фото (или синтетический вектор): какой товар, какая съёмка, откуда."""
    product: str
    session: str
    source: str
    known: bool = True                   # False — товара нет в каталоге (проверка отказа)


@dataclass
class Split:
    gallery: list[Item] = field(default_factory=list)
    tune: list[Item] = field(default_factory=list)
    test: list[Item] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def role_of(self) -> dict[str, str]:
        return {**{i.source: GALLERY for i in self.gallery}, **{i.source: TUNE for i in self.tune},
                **{i.source: TEST for i in self.test}}


def is_unknown_name(name: str) -> bool:
    return name.strip().lower().startswith(UNKNOWN_PREFIXES)


def split_by_sessions(items: Sequence[Item], gallery_session: str | None = None,
                      tune_sessions: Iterable[str] = ()) -> Split:
    """Эталоны — одна съёмка каждого известного товара (``gallery_session`` или первая по имени); запросы — остальные съёмки.
    Запросы из ``tune_sessions`` идут в подбор порогов, прочие — в контроль. Товары «не из каталога» в эталоны не попадают."""
    tune_set = {s for s in tune_sessions}
    split = Split()
    by_product: dict[str, dict[str, list[Item]]] = {}
    for it in items:
        by_product.setdefault(it.product, {}).setdefault(it.session, []).append(it)
    for product in sorted(by_product):
        sessions = by_product[product]
        known = all(i.known for group in sessions.values() for i in group)
        names = sorted(sessions)
        if known:
            if gallery_session is not None and gallery_session in sessions:
                g = gallery_session
            else:
                g = names[0]
                if gallery_session is not None:
                    split.warnings.append(f"«{product}»: нет съёмки «{gallery_session}», эталоном взята «{g}»")
            if g in tune_set:
                split.warnings.append(f"«{product}»: съёмка «{g}» — эталон, поэтому в подбор порогов не идёт")
            split.gallery.extend(sessions[g])
            queries = [n for n in names if n != g]
            if not queries:
                split.warnings.append(f"«{product}»: только одна съёмка — товар есть в каталоге, но запросов по нему нет")
        else:
            queries = names
        for n in queries:
            (split.tune if n in tune_set else split.test).extend(sessions[n])
    gallery_keys = {(i.product, i.session) for i in split.gallery}
    leaked = {(i.product, i.session) for i in split.tune + split.test} & gallery_keys
    assert not leaked, f"съёмка одновременно эталон и запрос: {sorted(leaked)}"       # защита от ошибки в коде выше
    return split


# ============================================================================ запросы и решение

@dataclass(frozen=True)
class Query:
    item: Item
    ranked: list[tuple[str, float]]      # (товар, лучший счёт по товару), лучший первым


def rank_queries(gallery: Sequence[Item], gallery_vecs: np.ndarray, queries: Sequence[Item],
                 query_vecs: np.ndarray) -> list[Query]:
    """Каталог из эталонов -> для каждого запроса кандидаты-товары (те же NumpyCatalogIndex и rank_products, что в приложении)."""
    index = NumpyCatalogIndex()
    for it, vec in zip(gallery, gallery_vecs, strict=True):
        index.add(it.product, vec[None, :])
    k = min(SEARCH_K, len(gallery))
    return [Query(it, rank_products(index.search(vec, top_k=k))) for it, vec in zip(queries, query_vecs, strict=True)]


@dataclass(frozen=True)
class Outcome:
    """Итог одного запроса при заданных порогах."""
    query: Query
    status: DetectionStatus
    answer: str | None
    top1: bool
    top5: bool

    @property
    def matched(self) -> bool:
        return self.status == DetectionStatus.MATCHED

    @property
    def wrong_matched(self) -> bool:
        return self.matched and (not self.query.item.known or self.answer != self.query.item.product)


def judge(q: Query, match_threshold: float, unknown_threshold: float, margin: float) -> Outcome:
    r = decide_match(q.ranked, match_threshold, unknown_threshold, margin)
    ids = [pid for pid, _ in q.ranked]
    truth = q.item.product if q.item.known else None
    return Outcome(q, r.status, r.product_id, truth is not None and ids[:1] == [truth], truth is not None and truth in ids[:5])


# ============================================================================ метрики и интервалы

METRICS: tuple[tuple[str, str], ...] = (
    ("top1", "Top-1: верный товар первым"),
    ("top5", "Top-5: верный товар среди пяти"),
    ("coverage", "Уверенный ответ (известные товары)"),
    ("risk", "Ошибки среди уверенных ответов"),
    ("reject", "Правильный отказ («не из каталога»)"),
    ("false_accept", "Ложное принятие чужого товара"),
)
# числитель и знаменатель каждой метрики — по счётчикам ниже
_COUNTERS = ("k", "k_top1", "k_top5", "k_matched", "k_wrong_matched", "u", "u_matched")
_RATIO = {"top1": ("k_top1", ("k",)), "top5": ("k_top5", ("k",)), "coverage": ("k_matched", ("k",)),
          "risk": (("k_wrong_matched", "u_matched"), ("k_matched", "u_matched")),
          "reject": (None, ("u",)), "false_accept": ("u_matched", ("u",))}


def _counts(outcomes: Sequence[Outcome]) -> dict[str, int]:
    c = dict.fromkeys(_COUNTERS, 0)
    for o in outcomes:
        if o.query.item.known:
            c["k"] += 1
            c["k_top1"] += o.top1
            c["k_top5"] += o.top5
            c["k_matched"] += o.matched
            c["k_wrong_matched"] += o.wrong_matched
        else:
            c["u"] += 1
            c["u_matched"] += o.matched
    return c


def _num_den(metric: str, c) -> tuple:
    num, den = _RATIO[metric]
    d = sum(c[k] for k in den)
    if metric == "reject":
        return c["u"] - c["u_matched"], d
    n = sum(c[k] for k in num) if isinstance(num, tuple) else c[num]
    return n, d


def zero_error_upper(n: int, confidence: float = 0.95) -> float:
    """Точная односторонняя верхняя граница доли при 0 событий из n (≈ 3/n — «правило трёх»)."""
    return 1.0 - (1.0 - confidence) ** (1.0 / n) if n > 0 else 1.0


@dataclass(frozen=True)
class Estimate:
    value: float | None                  # None — не из чего считать (знаменатель 0)
    low: float | None
    high: float | None
    num: int
    den: int


def bootstrap(outcomes: Sequence[Outcome], rounds: int = 1000, seed: int = 0) -> dict[str, Estimate]:
    """Оценка и 95-процентный интервал каждой метрики; bootstrap по ТОВАРАМ: выбираем товары с возвращением и берём все их фото."""
    groups: dict[str, list[Outcome]] = {}
    for o in outcomes:
        groups.setdefault(o.query.item.product, []).append(o)
    names = sorted(groups)
    table = np.array([[_counts(groups[n])[k] for k in _COUNTERS] for n in names], dtype=np.int64).reshape(len(names), len(_COUNTERS))
    total = _counts(outcomes)
    rng = np.random.default_rng(seed)
    if names:
        weights = rng.multinomial(len(names), np.full(len(names), 1.0 / len(names)), size=rounds)
        sums = weights @ table                                              # (rounds, счётчики)
    else:
        sums = np.zeros((0, len(_COUNTERS)), dtype=np.int64)
    columns = {k: sums[:, i] for i, k in enumerate(_COUNTERS)}
    result: dict[str, Estimate] = {}
    for metric, _ in METRICS:
        num, den = _num_den(metric, total)
        if den == 0:
            result[metric] = Estimate(None, None, None, num, den)
            continue
        bn, bd = _num_den(metric, columns)
        with np.errstate(invalid="ignore", divide="ignore"):
            ratios = np.where(bd > 0, bn / np.maximum(bd, 1), np.nan)
        ratios = ratios[~np.isnan(ratios)]
        low, high = (float(np.percentile(ratios, 2.5)), float(np.percentile(ratios, 97.5))) if ratios.size else (0.0, 1.0)
        value = num / den
        if num == 0:                                                        # ноль событий: честная верхняя граница
            low, high = 0.0, max(high, zero_error_upper(den))
        elif num == den:
            low, high = min(low, 1.0 - zero_error_upper(den)), 1.0
        result[metric] = Estimate(value, min(low, value), max(high, value), num, den)
    return result


# ============================================================================ подбор порогов на отдельной съёмке

@dataclass(frozen=True)
class Thresholds:
    match: float
    unknown: float
    margin: float
    how: str                             # откуда взяты — для отчёта


def tune_thresholds(queries: Sequence[Query], target_risk: float = TARGET_RISK) -> Thresholds | None:
    """Самая большая доля уверенных ответов при доле уверенных ошибок не выше цели — на съёмке для подбора. None — не из чего."""
    if not queries:
        return None
    best: tuple[float, float, float] | None = None                         # (coverage, match, margin): при равной доле — строже
    choice: tuple[float, float] | None = None
    for m in MATCH_GRID:
        for mg in MARGIN_GRID:
            c = _counts([judge(q, m, min(DEFAULT_UNKNOWN, m), mg) for q in queries])
            accepted = c["k_matched"] + c["u_matched"]
            wrong = c["k_wrong_matched"] + c["u_matched"]
            if accepted == 0 or wrong / accepted > target_risk:
                continue
            key = ((c["k_matched"] - c["k_wrong_matched"]) / max(c["k"], 1), m, mg)
            if best is None or key > best:
                best, choice = key, (m, mg)
    if choice is None:
        return Thresholds(MATCH_GRID[-1], DEFAULT_UNKNOWN, MARGIN_GRID[-1], "подбор не нашёл точки с ошибками ≤ цели — взяты самые строгие")
    return Thresholds(choice[0], min(DEFAULT_UNKNOWN, choice[0]), choice[1], "подобраны на съёмке для подбора")


# ============================================================================ прогон одной конфигурации

@dataclass
class ConfigResult:
    name: str
    title: str
    skipped: str | None = None           # причина пропуска (модели нет в среде и т. п.)
    thresholds: list[Thresholds] = field(default_factory=list)
    estimates: list[dict[str, Estimate]] = field(default_factory=list)          # по одной на набор порогов
    outcomes: list[list[Outcome]] = field(default_factory=list)
    per_session: dict[str, dict[str, Estimate]] = field(default_factory=dict)  # срезы по съёмкам (пороги — первый набор)
    confusions: list[tuple[str, str, int]] = field(default_factory=list)
    ms_per_image: float | None = None
    notes: list[str] = field(default_factory=list)


def evaluate(name: str, title: str, split: Split, tune_q: Sequence[Query], test_q: Sequence[Query],
             fixed: Thresholds, rounds: int, seed: int) -> ConfigResult:
    res = ConfigResult(name, title)
    sets = [fixed]
    tuned = tune_thresholds(tune_q)
    if tuned is not None:
        sets.append(tuned)
    for th in sets:
        outs = [judge(q, th.match, th.unknown, th.margin) for q in test_q]
        res.thresholds.append(th)
        res.outcomes.append(outs)
        res.estimates.append(bootstrap(outs, rounds, seed))
    main = res.outcomes[-1]                                                 # подобранные, если есть, иначе из настроек
    for s in sorted({o.query.item.session for o in main}):
        res.per_session[s] = bootstrap([o for o in main if o.query.item.session == s], rounds, seed)
    pairs = Counter((o.query.item.product, o.answer) for o in main if o.wrong_matched)
    top1_pairs = Counter((o.query.item.product, o.query.ranked[0][0]) for o in main
                         if o.query.item.known and not o.top1 and o.query.ranked)
    merged = pairs + top1_pairs
    res.confusions = [(a, b or "—", n) for (a, b), n in merged.most_common(10)]
    if not split.gallery:
        res.notes.append("нет эталонов")
    return res


# ============================================================================ синтетика

@dataclass(frozen=True)
class SyntheticSpec:
    name: str
    title: str
    sibling_spread: float                # насколько товары одной пары отличаются (меньше — похожее)
    noise: float                         # разброс фото внутри съёмки
    session_shift: float                 # сдвиг между съёмками (свет, телефон)


SYNTHETIC_SPECS = (
    SyntheticSpec("synthetic-easy", "Синтетика: лёгкая (товары сильно различаются)", 0.9, 0.35, 0.15),
    SyntheticSpec("synthetic-medium", "Синтетика: средняя", 0.4, 0.5, 0.3),
    SyntheticSpec("synthetic-hard", "Синтетика: трудная (похожие пары, сильный сдвиг съёмки)", 0.3, 0.55, 0.4),
)


def synthetic_dataset(spec: SyntheticSpec, products: int = 40, unknown: int = 12, sessions: Sequence[str] = ("A", "B", "C"),
                      per_session: int = 5, dim: int = 128, seed: int = 0) -> tuple[list[Item], np.ndarray]:
    """Пары похожих товаров (один бренд — разные вкусы), «чужие» товары: половина — из тех же брендов (трудные), половина — свои.
    Каждая съёмка сдвигает все фото товара в общую сторону (другой день, свет, телефон), сверху — шум каждого кадра."""
    rng = np.random.default_rng(seed)

    def unit(v):
        return v / np.linalg.norm(v, axis=-1, keepdims=True)

    def gauss(*shape):
        return rng.normal(size=(*shape, dim)) / math.sqrt(dim)

    brands = unit(gauss(products // 2 + 1))
    centers: dict[str, tuple[np.ndarray, bool]] = {}
    for i in range(products):
        centers[f"товар_{i:02d}"] = (unit(brands[i // 2] + spec.sibling_spread * gauss()), True)
    for j in range(unknown):
        base = brands[j % len(brands)] if j % 2 == 0 else unit(gauss())
        centers[f"чужой_{j:02d}"] = (unit(base + spec.sibling_spread * gauss()), False)
    items: list[Item] = []
    vecs: list[np.ndarray] = []
    for name, (center, known) in centers.items():
        for s in (sessions if known else sessions[1:]):                     # «чужие» товары — только в запросах
            shift = spec.session_shift * gauss()
            for k in range(per_session):
                items.append(Item(name, s, f"{name}/{s}/{k}", known))
                vecs.append(unit(center + shift + spec.noise * gauss()))
    return items, np.asarray(vecs, dtype=np.float32)


def run_synthetic(rounds: int = 1000, seed: int = 0) -> tuple[Split, list[ConfigResult]]:
    results = []
    split = Split()
    fixed = Thresholds(DEFAULT_MATCH, DEFAULT_UNKNOWN, DEFAULT_MARGIN, "из настроек (configs/config.yaml)")
    for spec in SYNTHETIC_SPECS:
        items, vecs = synthetic_dataset(spec, seed=seed)
        split = split_by_sessions(items, gallery_session="A", tune_sessions=("B",))
        by_src = {it.source: v for it, v in zip(items, vecs)}

        def stack(group):
            return np.stack([by_src[i.source] for i in group])

        tune_q = rank_queries(split.gallery, stack(split.gallery), split.tune, stack(split.tune))
        test_q = rank_queries(split.gallery, stack(split.gallery), split.test, stack(split.test))
        results.append(evaluate(spec.name, spec.title, split, tune_q, test_q, fixed, rounds, seed))
    return split, results


# ============================================================================ настоящие фото

def scan_photos(root: Path) -> tuple[list[Item], list[str]]:
    """``<товар>/<съёмка>/*.jpg`` -> список фото. Неподходящие файлы и пустые папки — в предупреждения (фото не трогаются)."""
    items: list[Item] = []
    warnings: list[str] = []
    if not root.is_dir():
        raise FileNotFoundError(f"папка с набором не найдена: {root}")
    for product_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        known = not is_unknown_name(product_dir.name)
        sessions = sorted(p for p in product_dir.iterdir() if p.is_dir())
        loose = [p for p in product_dir.iterdir() if p.is_file()]
        if loose:
            warnings.append(f"«{product_dir.name}»: {len(loose)} файл(ов) лежат прямо в папке товара, без папки съёмки — пропущены")
        for s in sessions:
            photos = sorted(p for p in s.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES)
            other = [p for p in s.iterdir() if p.is_file() and p.suffix.lower() not in PHOTO_SUFFIXES]
            if other:
                warnings.append(f"«{product_dir.name}/{s.name}»: {len(other)} файл(ов) не JPG/PNG (например, {other[0].name}) — пропущены")
            for p in photos:
                items.append(Item(product_dir.name, s.name, str(p.relative_to(root).as_posix()), known))
    return items, warnings


# --- обработка кадра перед моделью (одна и та же для эталонов и запросов)

def full_frame(image: np.ndarray) -> np.ndarray:
    return image


def center_crop(image: np.ndarray, fraction: float = 0.6) -> np.ndarray:
    """Центральная часть кадра (по каждой стороне ``fraction``): товар в руке обычно в середине, фон по краям отрезается."""
    h, w = image.shape[:2]
    ch, cw = max(1, round(h * fraction)), max(1, round(w * fraction))
    y, x = (h - ch) // 2, (w - cw) // 2
    return image[y:y + ch, x:x + cw]


def pad_square(image: np.ndarray) -> np.ndarray:
    """Дополнение до квадрата серым: процессор DINOv2 вырезает центральный квадрат, и края вытянутой упаковки терялись бы (§2)."""
    h, w = image.shape[:2]
    side = max(h, w)
    out = np.full((side, side, 3), 128, dtype=image.dtype)
    y, x = (side - h) // 2, (side - w) // 2
    out[y:y + h, x:x + w] = image[..., :3]
    return out


@dataclass(frozen=True)
class ModelConfig:
    name: str
    title: str
    model: str                           # ключ модели в MODELS
    transform: str                       # "full" | "center" | "pad" | "yolo"


MODELS = {
    # ключ: (репозиторий HuggingFace, вид модели, библиотеки)
    "dinov2-base": ("facebook/dinov2-base", "dinov2", ("torch", "transformers")),
    "dinov2-small": ("facebook/dinov2-small", "dinov2", ("torch", "transformers")),
    "siglip2-base": ("google/siglip2-base-patch16-224", "siglip", ("torch", "transformers")),
}

PHOTO_CONFIGS = (
    ModelConfig("dinov2-base/full", "DINOv2-base, целый кадр (как сейчас)", "dinov2-base", "full"),
    ModelConfig("dinov2-base/center", "DINOv2-base, центральный кроп 60 %", "dinov2-base", "center"),
    ModelConfig("dinov2-base/pad", "DINOv2-base, дополнение до квадрата (края не теряются)", "dinov2-base", "pad"),
    ModelConfig("dinov2-small/full", "DINOv2-small, целый кадр", "dinov2-small", "full"),
    ModelConfig("siglip2-base/full", "SigLIP 2 base, только визуальная часть, целый кадр", "siglip2-base", "full"),
    ModelConfig("dinov2-base/yolo", "DINOv2-base, кроп по детектору YOLO (одна рамка; иначе целый кадр)", "dinov2-base", "yolo"),
)


LOCAL_WEIGHTS = {"dinov2-base": "models/embedder/dinov2_base_retail_ft"}     # дообученные веса приложения, если есть


def _missing_libs(libs: Iterable[str]) -> list[str]:
    return [lib for lib in libs if importlib.util.find_spec(lib) is None]


def _hub_cached(repo: str) -> bool:
    """Скачаны ли веса в кэш HuggingFace (папка ``models--владелец--имя/snapshots/*/config.json``). Без импорта huggingface_hub:
    он приходит вместе с transformers и в requirements.txt отдельно не объявлен."""
    cache = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"), "hub")
    snapshots = Path(cache) / ("models--" + repo.replace("/", "--")) / "snapshots"
    return snapshots.is_dir() and any((d / "config.json").is_file() for d in snapshots.iterdir())


def model_availability(key: str, allow_download: bool) -> str | None:
    """None — модель можно запустить; иначе — понятная причина пропуска."""
    repo, _, libs = MODELS[key]
    missing = _missing_libs(libs)
    if missing:
        return f"не установлены библиотеки: {', '.join(missing)} (в рабочей среде тяжёлые ML-пакеты не ставились)"
    local = PROJECT_ROOT / LOCAL_WEIGHTS.get(key, "-")
    if not allow_download and not local.is_dir() and not _hub_cached(repo):
        return f"веса {repo} не скачаны (запустите с --allow-download, нужен интернет, около 0,1–1 ГБ)"
    return None


def detector_availability(weights: Path) -> str | None:
    if not weights.is_file():
        return f"нет файла весов детектора {weights.relative_to(PROJECT_ROOT) if weights.is_relative_to(PROJECT_ROOT) else weights}"
    missing = _missing_libs(("ultralytics", "torch"))
    if missing:
        return f"не установлены библиотеки: {', '.join(missing)}"
    return None


class HFVisionEmbedder:
    """Эмбеддер HuggingFace для сравнения. Для DINOv2 — CLS-токен (как DinoV2Embedder приложения); для SigLIP 2 — только
    визуальная башня и её итоговый вектор (pooler_output = get_image_features). Векторы L2-нормализуются."""

    def __init__(self, repo: str, kind: str, device: str = "cuda"):
        import torch
        from transformers import AutoImageProcessor

        self._torch = torch
        self.device = device if (device != "cuda" or torch.cuda.is_available()) else "cpu"
        self.kind = kind
        self._processor = AutoImageProcessor.from_pretrained(repo)
        if kind == "siglip":
            try:
                from transformers import SiglipVisionModel
                model = SiglipVisionModel.from_pretrained(repo)
            except Exception:                                               # другая архитектура в карточке — общая загрузка
                from transformers import AutoModel
                model = AutoModel.from_pretrained(repo)
        else:
            from transformers import AutoModel
            model = AutoModel.from_pretrained(repo)
        self._model = model.to(self.device).eval()

    def embed(self, images: list[np.ndarray]) -> np.ndarray:
        inputs = self._processor(images=images, return_tensors="pt").to(self.device)
        with self._torch.no_grad():
            if self.kind == "siglip":
                if hasattr(self._model, "get_image_features"):
                    vecs = self._model.get_image_features(**inputs)
                else:
                    vecs = self._model(**inputs).pooler_output
            else:
                vecs = self._model(**inputs).last_hidden_state[:, 0, :]
        vecs = vecs.float().cpu().numpy()
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms


def _load_embedder(key: str, device: str):
    repo, kind, _ = MODELS[key]
    if key == "dinov2-base":                                                # боевой код приложения — те же векторы, что в системе
        from app.pipeline.embedder import DinoV2Embedder
        return DinoV2Embedder(weights_path=LOCAL_WEIGHTS["dinov2-base"], embedding_dim=768, device=device)
    return HFVisionEmbedder(repo, kind, device)


@dataclass
class YoloCropper:
    """Кроп по детектору: ровно одна рамка — кроп; ноль или несколько — целый кадр (выбирать рамку сами не будем, §3.2)."""
    detector: object
    single: int = 0
    none: int = 0
    several: int = 0

    def __call__(self, image: np.ndarray) -> np.ndarray:
        from app.pipeline.imageio import crop_box

        boxes = self.detector.detect(image)
        if len(boxes) == 1:
            self.single += 1
            b = boxes[0].bbox
            crop = crop_box(image, b.x1, b.y1, b.x2, b.y2)
            return crop if crop.size else image
        if boxes:
            self.several += 1
        else:
            self.none += 1
        return image

    def note(self) -> str:
        total = self.single + self.none + self.several
        return (f"детектор: одна рамка на {self.single} из {total} кадров, нет рамки — {self.none}, несколько — {self.several} "
                f"(в двух последних случаях взят целый кадр)")


def embed_items(items: Sequence[Item], root: Path, embed: Callable[[list[np.ndarray]], np.ndarray],
                transform: Callable[[np.ndarray], np.ndarray], batch: int = 16) -> tuple[np.ndarray, float, list[str]]:
    """Векторы фото (порядок как у ``items``), среднее время на фото (мс) и список нечитаемых файлов (их вектор — нулевой)."""
    from app.pipeline.imageio import load_rgb

    out: list[np.ndarray] = []
    bad: list[str] = []
    started = time.perf_counter()
    dim = None
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        images, ok = [], []
        for it in chunk:
            try:
                images.append(transform(load_rgb(root / it.source)))
                ok.append(True)
            except Exception:
                bad.append(it.source)
                ok.append(False)
        vecs = embed(images) if images else np.zeros((0, dim or 1), dtype=np.float32)
        dim = vecs.shape[1] if len(vecs) else dim
        it_vecs = iter(vecs)
        for good in ok:
            out.append(next(it_vecs) if good else None)
    dim = dim or 1
    matrix = np.stack([v if v is not None else np.zeros(dim, dtype=np.float32) for v in out]) if out else np.zeros((0, dim))
    elapsed = (time.perf_counter() - started) * 1000 / max(len(items), 1)
    return matrix.astype(np.float32), elapsed, bad


def run_photos(root: Path, configs: Sequence[ModelConfig] = PHOTO_CONFIGS, gallery_session: str | None = None,
               tune_sessions: Sequence[str] = (), allow_download: bool = False, device: str = "cuda",
               detector_weights: Path | None = None, rounds: int = 1000, seed: int = 0,
               loader: Callable[[str, str], object] | None = None,
               availability: Callable[[str], str | None] | None = None,
               detector_loader: Callable[[Path], object] | None = None) -> tuple[Split, list[ConfigResult], list[str]]:
    """Прогон всех конфигураций на наборе. ``loader``/``availability``/``detector_loader`` подменяются в тестах."""
    items, warnings = scan_photos(root)
    split = split_by_sessions(items, gallery_session, tune_sessions)
    warnings += split.warnings
    if not split.gallery:
        raise ValueError("в наборе нет ни одного эталона: нужна папка <товар>/<съёмка>/ с фото хотя бы у одного товара")
    if not any(i.known for i in split.tune + split.test):
        raise ValueError("нет запросов: у каждого товара нужно хотя бы две съёмки (первая — эталоны, остальные — запросы)")
    if not allow_download:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")                       # ничего не скачивать молча
    availability = availability or (lambda key: model_availability(key, allow_download))
    loader = loader or _load_embedder
    weights = detector_weights or PROJECT_ROOT / "models" / "detector" / "sku110k_yolo11n.pt"
    fixed = Thresholds(DEFAULT_MATCH, DEFAULT_UNKNOWN, DEFAULT_MARGIN, "из настроек (configs/config.yaml)")
    transforms = {"full": full_frame, "center": center_crop, "pad": pad_square}
    embedders: dict[str, object] = {}
    results: list[ConfigResult] = []
    for cfg in configs:
        reason = availability(cfg.model)
        cropper = None
        if reason is None and cfg.transform == "yolo":
            reason = detector_availability(weights) if detector_loader is None else None
            if reason is None:
                try:
                    det = detector_loader(weights) if detector_loader else _load_detector(weights)
                    cropper = YoloCropper(det)
                except Exception as exc:                                    # pragma: no cover - зависит от среды
                    reason = f"детектор не загрузился: {exc}"
        if reason is None and cfg.model not in embedders:
            try:
                embedders[cfg.model] = loader(cfg.model, device)
            except Exception as exc:
                reason = f"модель не загрузилась: {type(exc).__name__}: {exc}"
        if reason is not None:
            results.append(ConfigResult(cfg.name, cfg.title, skipped=reason))
            continue
        embed = embedders[cfg.model].embed
        transform = cropper or transforms[cfg.transform]
        g_vecs, ms_g, bad_g = embed_items(split.gallery, root, embed, transform)
        t_vecs, ms_t, bad_t = embed_items(split.tune, root, embed, transform) if split.tune else (np.zeros((0, 1)), 0.0, [])
        q_vecs, ms_q, bad_q = embed_items(split.test, root, embed, transform)
        bad = set(bad_g + bad_t + bad_q)

        def keep(group, vecs):
            idx = [i for i, it in enumerate(group) if it.source not in bad]
            return [group[i] for i in idx], vecs[idx] if len(idx) else vecs[:0]

        gal, g_vecs = keep(split.gallery, g_vecs)
        tun, t_vecs = keep(split.tune, t_vecs)
        tst, q_vecs = keep(split.test, q_vecs)
        tune_q = rank_queries(gal, g_vecs, tun, t_vecs) if tun else []
        test_q = rank_queries(gal, g_vecs, tst, q_vecs)
        res = evaluate(cfg.name, cfg.title, split, tune_q, test_q, fixed, rounds, seed)
        n_all = len(split.gallery) + len(split.tune) + len(split.test)
        res.ms_per_image = (ms_g * len(split.gallery) + ms_t * len(split.tune) + ms_q * len(split.test)) / max(n_all, 1)
        if bad:
            res.notes.append(f"не прочитано фото: {len(bad)} (например, {sorted(bad)[0]}) — исключены")
        if cropper is not None:
            res.notes.append(cropper.note())
        results.append(res)
    return split, results, warnings


def _load_detector(weights: Path):                                         # pragma: no cover - нужны ultralytics и веса
    from app.pipeline.detector import YoloShelfDetector
    return YoloShelfDetector(str(weights), confidence_threshold=0.35)


# ============================================================================ отчёт

def pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.1f} %"


def cell(e: Estimate) -> str:
    if e.value is None:
        return "—"
    return f"{pct(e.value)} [{pct(e.low)}; {pct(e.high)}] ({e.num}/{e.den})"


def risk_verdict(e: Estimate) -> str:
    """Честная фраза про главную цифру: можно ли утверждать «≤ 1 %»."""
    if e.value is None or e.den == 0:
        return "уверенных ответов нет — про ошибки среди уверенных сказать нечего"
    if e.high is not None and e.high <= TARGET_RISK:
        return f"цель «≤ 1 %» подтверждена: верхняя граница {pct(e.high)}"
    if e.num == 0:
        return (f"ошибок нет, но уверенных ответов всего {e.den}: доля ошибок может доходить до {pct(e.high)}; "
                f"чтобы утверждать «≤ 1 %», нужно не меньше {CONFIDENT_FOR_1PCT} уверенных ответов без ошибки")
    return f"цель «≤ 1 %» не подтверждена: ошибок {e.num} из {e.den}, верхняя граница {pct(e.high)}"


def build_report(mode: str, split: Split, results: Sequence[ConfigResult], warnings: Sequence[str] = (), rounds: int = 1000,
                 source: str = "") -> str:
    lines: list[str] = []
    w = lines.append
    w(f"# Сравнение распознавания по виду — {'СИНТЕТИКА (проверка инструмента)' if mode == 'synthetic' else 'набор фото'}")
    w("")
    if mode == "synthetic":
        w("> **Это не оценка моделей.** Векторы искусственные: отчёт показывает только, что инструмент правильно считает "
          "цифры, интервалы и разделение по съёмкам. Настоящие цифры появятся после съёмки набора (docs/benchmark_photo_guide.md).")
    else:
        w(f"Набор: `{source}`")
    w("")
    known = {i.product for i in split.gallery}
    unknown = {i.product for i in split.test + split.tune if not i.known}
    w(f"Товаров в каталоге (эталоны): {len(known)}; товаров «не из каталога»: {len(unknown)}. "
      f"Фото: эталоны {len(split.gallery)}, подбор порогов {len(split.tune)}, контроль {len(split.test)}.")
    w("Съёмка эталонов никогда не попадает в запросы; цифры ниже — только на **контрольных** съёмках.")
    w(f"Интервалы — 95 %, bootstrap по товарам ({rounds} повторов); при нуле ошибок верхняя граница — точное «правило трёх».")
    w("")
    for r in results:
        w(f"## {r.title}")
        w("")
        if r.skipped:
            w(f"**Пропущено:** {r.skipped}.")
            w("")
            continue
        for th, est in zip(r.thresholds, r.estimates):
            w(f"Пороги: совпадение {th.match:.2f}, «неизвестно» {th.unknown:.2f}, запас {th.margin:.2f} — {th.how}.")
            w("")
            w("| Показатель | Значение [95 % интервал] (число/из) |")
            w("|---|---|")
            for key, label in METRICS:
                w(f"| {label} | {cell(est[key])} |")
            w("")
            w(f"Вывод по ошибкам: {risk_verdict(est['risk'])}.")
            w("")
        if len(r.per_session) > 1:
            w("По контрольным съёмкам (последний набор порогов):")
            w("")
            w("| Съёмка | Top-1 | Уверенный ответ | Ошибки среди уверенных |")
            w("|---|---|---|---|")
            for s, est in r.per_session.items():
                w(f"| {s} | {cell(est['top1'])} | {cell(est['coverage'])} | {cell(est['risk'])} |")
            w("")
        if r.confusions:
            w("Чаще всего путает (товар → за что принят; ошибки первого места и уверенные ошибки):")
            w("")
            for a, b, n in r.confusions[:5]:
                w(f"- {a} → {b}: {n}")
            w("")
        if r.ms_per_image is not None:
            w(f"Скорость: около {r.ms_per_image:.0f} мс на фото (чтение, обработка, модель; без OCR и штрихкода).")
            w("")
        for note in r.notes:
            w(f"- {note}")
        if r.notes:
            w("")
    ok = [r for r in results if not r.skipped]
    if ok:
        w("## Сводка")
        w("")
        w("| Конфигурация | Top-1 | Top-5 | Уверенный ответ | Ошибки среди уверенных | Правильный отказ |")
        w("|---|---|---|---|---|---|")
        for r in ok:
            e = r.estimates[-1]
            w(f"| {r.title} | {pct(e['top1'].value)} | {pct(e['top5'].value)} | {pct(e['coverage'].value)} | "
              f"{pct(e['risk'].value)} (до {pct(e['risk'].high)}) | {pct(e['reject'].value)} |")
        w("")
        w("Различие двух конфигураций можно считать настоящим, только если их интервалы почти не пересекаются.")
    if warnings:
        w("")
        w("## Предупреждения о наборе")
        w("")
        for x in warnings[:50]:
            w(f"- {x}")
    w("")
    return "\n".join(lines)


def write_queries_csv(results: Sequence[ConfigResult], path: Path) -> None:
    """Построчно по каждому запросу (для разбора в Excel): конфигурация, фото, правда, ответ, статус, счёты."""
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.writer(fh, delimiter=";")
        wr.writerow(["конфигурация", "пороги", "фото", "товар", "в_каталоге", "съёмка", "первый_кандидат", "счёт_1",
                     "второй_кандидат", "счёт_2", "статус", "ответ", "top1_верно", "top5_верно", "уверенная_ошибка"])
        for r in results:
            for th, outs in zip(r.thresholds, r.outcomes):
                for o in outs:
                    rk = o.query.ranked
                    wr.writerow([r.name, f"{th.match:.2f}/{th.unknown:.2f}/{th.margin:.2f}", o.query.item.source,
                                 o.query.item.product, "да" if o.query.item.known else "нет", o.query.item.session,
                                 rk[0][0] if rk else "", f"{rk[0][1]:.4f}" if rk else "",
                                 rk[1][0] if len(rk) > 1 else "", f"{rk[1][1]:.4f}" if len(rk) > 1 else "",
                                 o.status.value, o.answer or "", int(o.top1), int(o.top5), int(o.wrong_matched)])


def save(out_dir: Path, report: str, results: Sequence[ConfigResult]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rep, rows = out_dir / "report.md", out_dir / "queries.csv"
    rep.write_text(report, encoding="utf-8")
    write_queries_csv(results, rows)
    return rep, rows


# ============================================================================ командная строка

def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):                                 # русский текст в консоли Windows
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="python -m ml.benchmark_identify", description=__doc__.split("\n\n")[1].strip())
    sub = p.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("synthetic", help="проверка инструмента на искусственных векторах")
    s.add_argument("--out", default="data/benchmark/synthetic")
    s.add_argument("--rounds", type=int, default=1000)
    s.add_argument("--seed", type=int, default=0)
    sub.add_parser("list", help="какие конфигурации можно запустить в этой среде")
    f = sub.add_parser("photos", help="настоящий набор: <товар>/<съёмка>/*.jpg")
    f.add_argument("--photos", required=True, help="папка набора")
    f.add_argument("--out", default="data/benchmark/photos")
    f.add_argument("--gallery-session", default=None, help="имя съёмки-эталона (по умолчанию — первая по имени у каждого товара)")
    f.add_argument("--tune-session", action="append", default=[], help="съёмка для подбора порогов (можно несколько раз)")
    f.add_argument("--configs", default="", help="через запятую, например dinov2-base/full,dinov2-small/full (по умолчанию все)")
    f.add_argument("--allow-download", action="store_true", help="разрешить скачать веса моделей (нужен интернет)")
    f.add_argument("--device", default="cuda", help="cuda или cpu (без видеокарты берётся cpu)")
    f.add_argument("--detector-weights", default=None)
    f.add_argument("--rounds", type=int, default=1000)
    f.add_argument("--seed", type=int, default=0)
    a = p.parse_args(argv)

    if a.mode == "list":
        weights = PROJECT_ROOT / "models" / "detector" / "sku110k_yolo11n.pt"
        for cfg in PHOTO_CONFIGS:
            reason = model_availability(cfg.model, allow_download=False)
            if reason is None and cfg.transform == "yolo":
                reason = detector_availability(weights)
            print(f"{cfg.name:22} {'готово' if reason is None else 'пропуск: ' + reason}")
        return 0
    if a.mode == "synthetic":
        split, results = run_synthetic(a.rounds, a.seed)
        report = build_report("synthetic", split, results, rounds=a.rounds)
        rep, rows = save(Path(a.out), report, results)
        print(report)
        print(f"\nОтчёт: {rep}\nПострочно: {rows}")
        return 0
    chosen = PHOTO_CONFIGS
    if a.configs:
        wanted = [c.strip() for c in a.configs.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in {x.name for x in PHOTO_CONFIGS}]
        if unknown:
            print(f"Неизвестные конфигурации: {', '.join(unknown)}. Список: python -m ml.benchmark_identify list")
            return 2
        chosen = tuple(c for c in PHOTO_CONFIGS if c.name in wanted)
    try:
        split, results, warnings = run_photos(Path(a.photos), chosen, a.gallery_session, a.tune_session, a.allow_download,
                                              a.device, Path(a.detector_weights) if a.detector_weights else None, a.rounds, a.seed)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Ошибка: {exc}")
        return 1
    report = build_report("photos", split, results, warnings, a.rounds, source=a.photos)
    rep, rows = save(Path(a.out), report, results)
    print(report)
    print(f"\nОтчёт: {rep}\nПострочно: {rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
