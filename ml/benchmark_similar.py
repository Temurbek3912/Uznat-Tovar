"""
Похожие товары и объёмы: как часто «Узнать товар» путает товары одной марки и разные объёмы одного товара (замер 2026-11-21).

    .venv/Scripts/python.exe -m ml.benchmark_similar                     # рабочий индекс и база, отчёт в data/benchmark/similar
    .venv/Scripts/python.exe -m ml.benchmark_similar --out D:/замер      # другой каталог отчёта

Только ЧИТАЕТ: векторы из рабочего индекса (models/index, без пересчёта и без видеокарты), товары и фото — из базы. Ничего не
пишет ни в индекс, ни в базу.

Набор. Каждое фото каталога по очереди — «снимок покупателя», остальные — каталог («оставить одно»). Как в
ml/benchmark_no_copies.py, из каталога для снимка убираются фото того же товара со сходством больше --copy (0,93): в открытой
базе лицевое фото часто — обрезанная копия другого фото того же товара, а покупатель снимает свою, новую фотографию.
Считаются только снимки, у товара которых осталось другое фото. Учитываются только эталоны, которые участвуют в распознавании:
фото выведено из эталонов или товар в архиве — его вектор не в счёт (как в программе).

Группы:
  * «лицевые» — фото, загруженные при импорте как лицевая сторона (имя исходного файла …/front…): так снимает покупатель;
  * «одна марка» — у марки товара в базе ≥ 2 товаров (разные вкусы, виды, объёмы);
  * «другой объём» — в базе есть товар с теми же словами названия и марки, но другим объёмом или без указания объёма
    (app/services/product_size.py: «Coca-Cola Classic 0,5 л» и «… 1,5 л»).
Ошибки первого места делятся на «тот же товар, другой объём», «та же марка, другой товар» и прочие.

Решение «уверен / похоже / не знаю» — ровно decide_match (как в программе) по сетке «порог × запас»; для каждой точки — доля
уверенных ответов, сколько из них неверных и как часто «узнаётся» товар, которого НЕТ в базе (из каталога убраны все его фото).

Важно: это фото каталога из открытой базы, а не снимки магазина (docs/benchmark_photo_guide.md). Цифры — для сравнения
способов между собой на одном наборе; настоящие пороги проверяются на своих фото (ml/evaluate_matching.py).
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from app.core.schemas import DetectionStatus
from app.pipeline.matcher import decide_match

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COPY_SIMILARITY = 0.93
DEPTH = 25                                       # товаров в выдаче на снимок (как candidates_k в программе)
MATCH_GRID = (0.84, 0.85, 0.86, 0.87, 0.88, 0.90)
MARGIN_GRID = (0.0, 0.02, 0.03, 0.04, 0.05, 0.06)
NO_BRAND = {"", "не указан", "не указано", "-"}


@dataclass(frozen=True)
class Item:
    """Товар для замера: id, название, марка."""
    id: str
    name: str
    brand: str = ""


@dataclass
class Query:
    index: int                                   # номер снимка (вектора) в наборе
    truth: str                                   # товар снимка
    ranked: list[tuple[str, float]]              # (товар, лучший счёт), лучший первым


@dataclass
class Summary:
    counts: Counter = field(default_factory=Counter)

    def rate(self, group: str, key: str) -> float:
        n = self.counts[group, "n"]
        return self.counts[group, key] / n if n else 0.0


def _brand(item: Item) -> str | None:
    b = (item.brand or "").strip().lower()
    return None if b in NO_BRAND else b


def size_siblings(items: Sequence[Item]) -> dict[str, set[str]]:
    """Товар -> другие товары с теми же словами названия и марки, но другим (или не указанным) объёмом."""
    from app.services.product_size import name_without_size, same_size, size_of     # noqa: PLC0415
    keyed: dict[frozenset, list[Item]] = {}
    for it in items:
        b = it.brand if _brand(it) else ""
        base = name_without_size(f"{it.name} {b}")
        if base:
            keyed.setdefault(base, []).append(it)
    out: dict[str, set[str]] = {}
    for group in keyed.values():
        for a in group:
            for b in group:
                if a.id != b.id and not same_size(size_of(a.name), size_of(b.name)):
                    out.setdefault(a.id, set()).add(b.id)
    return out


def brand_families(items: Sequence[Item]) -> set[str]:
    """Товары, у марки которых в базе не меньше двух товаров."""
    counts = Counter(_brand(it) for it in items if _brand(it))
    return {it.id for it in items if _brand(it) and counts[_brand(it)] >= 2}


def _ranked(row: np.ndarray, owners: np.ndarray, allowed: np.ndarray, depth: int) -> list[tuple[str, float]]:
    best: dict[str, float] = {}
    for j in np.argsort(-row):
        if allowed[j] and owners[j] not in best:
            best[str(owners[j])] = float(row[j])
            if len(best) >= depth:
                break
    return list(best.items())


def leave_one_out(vectors: np.ndarray, owners: Sequence[str], copy: float = COPY_SIMILARITY, depth: int = DEPTH) -> list[Query]:
    """Каждый вектор — снимок; из каталога убраны он сам и копии того же товара (сходство > copy)."""
    owners = np.asarray(owners)
    sims = vectors @ vectors.T
    out = []
    for i in range(len(owners)):
        same = owners == owners[i]
        allowed = np.ones(len(owners), bool)
        allowed[i] = False
        allowed &= ~(same & (sims[i] > copy))
        if not (allowed & same).any():
            continue
        out.append(Query(i, str(owners[i]), _ranked(sims[i], owners, allowed, depth)))
    return out


def out_of_catalog(vectors: np.ndarray, owners: Sequence[str], depth: int = DEPTH) -> list[Query]:
    """Каждый вектор — снимок товара, которого НЕТ в каталоге: убраны все фото этого товара."""
    owners = np.asarray(owners)
    sims = vectors @ vectors.T
    return [Query(i, str(owners[i]), _ranked(sims[i], owners, owners != owners[i], depth)) for i in range(len(owners))]


def summarize(queries: Sequence[Query], items: dict[str, Item], front: Sequence[bool],
              siblings: dict[str, set[str]], families: set[str]) -> Summary:
    """top-1 / top-5 по группам и виды ошибок первого места."""
    s = Summary()
    for q in queries:
        order = [pid for pid, _ in q.ranked]
        groups = ["all"] + (["front"] if front[q.index] else []) + (["family"] if q.truth in families else []) \
            + (["size"] if q.truth in siblings else [])
        top = order[0] if order else None
        for g in groups:
            s.counts[g, "n"] += 1
            s.counts[g, "top1"] += top == q.truth
            s.counts[g, "top5"] += q.truth in order[:5]
            if top is not None and top != q.truth:
                if top in siblings.get(q.truth, ()):
                    s.counts[g, "err_size"] += 1
                elif _brand(items[top]) and _brand(items[top]) == _brand(items[q.truth]):
                    s.counts[g, "err_brand"] += 1
                else:
                    s.counts[g, "err_other"] += 1
    return s


@dataclass(frozen=True)
class Point:
    match: float
    margin: float
    confident: int                 # ответов «найдено»
    wrong: int                     # из них — другой товар
    wrong_size: int                # из них — тот же товар другого объёма
    front_confident: int
    front_wrong: int
    stranger_accepted: float       # доля снимков товара НЕ из базы, «узнанных» уверенно
    n: int


def decision_grid(queries: Sequence[Query], strangers: Sequence[Query], front: Sequence[bool], siblings: dict[str, set[str]],
                  unknown: float, match_grid=MATCH_GRID, margin_grid=MARGIN_GRID) -> list[Point]:
    out = []
    for match in match_grid:
        for margin in margin_grid:
            c = Counter()
            for q in queries:
                d = decide_match(q.ranked, match, unknown, margin)
                if d.status != DetectionStatus.MATCHED:
                    continue
                bad = d.product_id != q.truth
                c["m"] += 1
                c["w"] += bad
                c["ws"] += d.product_id in siblings.get(q.truth, ())
                c["fm"] += bool(front[q.index])
                c["fw"] += bool(front[q.index]) and bad
            acc = sum(decide_match(q.ranked, match, unknown, margin).status == DetectionStatus.MATCHED for q in strangers)
            out.append(Point(match, margin, c["m"], c["w"], c["ws"], c["fm"], c["fw"],
                             acc / len(strangers) if strangers else 0.0, len(queries)))
    return out


def _pct(x: float) -> str:
    return f"{100 * x:.1f} %".replace(".", ",")


def render(summary: Summary, grid: list[Point], now: tuple[float, float], unknown: float, n_vectors: int, n_products: int,
           siblings: dict[str, set[str]], items: dict[str, Item]) -> str:
    lines = ["# Похожие товары и объёмы — замер (ml/benchmark_similar.py)", "",
             f"Векторов в распознавании: {n_vectors}, товаров: {n_products}. Снимков (без копий своего товара): "
             f"{summary.counts['all', 'n']}. Фото каталога, не снимки магазина.", "",
             "## Верный товар первым и в первой пятёрке", "",
             "| Группа | Снимков | Первым | В пятёрке | Ошибка «другой объём» | Ошибка «та же марка» | Прочие ошибки |",
             "|---|---|---|---|---|---|---|"]
    names = {"all": "все", "front": "лицевые", "family": "одна марка (≥ 2 товаров)", "size": "есть другой объём"}
    for g, title in names.items():
        c = summary.counts
        if c[g, "n"]:
            lines.append(f"| {title} | {c[g, 'n']} | {_pct(summary.rate(g, 'top1'))} | {_pct(summary.rate(g, 'top5'))} | "
                         f"{c[g, 'err_size']} | {c[g, 'err_brand']} | {c[g, 'err_other']} |")
    lines += ["", f"## «Найдено» (уверенно): порог совпадения × запас (порог «не знаю» {unknown})", "",
              "Запас — правило decide_match: второй по близости ДРУГОЙ товар ближе запаса к первому -> «похоже» с вариантами.", "",
              "| Порог | Запас | Уверенных | Из них неверно | Другой объём | Лицевые: уверенных / неверно | Товар не из базы принят |",
              "|---|---|---|---|---|---|---|"]
    for p in grid:
        mark = " **(сейчас)**" if (p.match, p.margin) == now else ""
        lines.append(f"| {p.match:.2f}{mark} | {p.margin:.2f} | {_pct(p.confident / p.n if p.n else 0)} ({p.confident}) | "
                     f"{p.wrong} ({_pct(p.wrong / p.confident if p.confident else 0)}) | {p.wrong_size} | "
                     f"{p.front_confident} / {p.front_wrong} | {_pct(p.stranger_accepted)} |")
    pairs = sorted({tuple(sorted((a, b))) for a, bs in siblings.items() for b in bs})
    if pairs:
        lines += ["", "## Товары «одно название — разный объём» в базе", "",
                  "Объём по виду почти не различается: при близких кандидатах программа просит уточнить объём. Если объём в "
                  "названии не указан у одного из пары, это стоит поправить в карточке товара.", ""]
        lines += [f"* {items[a].name} — {items[b].name}" for a, b in pairs]
    return "\n".join(lines) + "\n"


def _load_from_service(index_path: str):
    """Рабочий индекс (только чтение) и база -> векторы, владельцы, флаги «лицевая», товары."""
    from app.db.models import Product, ProductImage                 # noqa: PLC0415
    from app.db.session import SessionLocal                         # noqa: PLC0415
    from app.pipeline.matcher import FaissCatalogIndex              # noqa: PLC0415
    from app.services.product_images import PhotoRegistry           # noqa: PLC0415
    index = FaissCatalogIndex.load(index_path)
    db = SessionLocal()
    try:
        products = {p.id: p for p in db.query(Product).all()}
        excluded = set(PhotoRegistry(db).excluded_sources())
        names = {(pid, src): (orig or "") for pid, src, orig in
                 db.query(ProductImage.product_id, ProductImage.source_ref, ProductImage.original_name).all()}
    finally:
        db.close()
    vectors = index._index.reconstruct_n(0, index._index.ntotal)
    keep = [i for i, (pid, src) in enumerate(zip(index._ids, index._sources))
            if pid in products and products[pid].status != "archived" and (pid, src) not in excluded]
    owners = [index._ids[i] for i in keep]
    front = ["/front" in names.get((index._ids[i], index._sources[i]), "") for i in keep]
    items = {pid: Item(pid, p.name, p.brand or "") for pid, p in products.items() if pid in set(owners)}
    return vectors[keep], owners, front, items


PROJECTION_WARNING = (
    "> **ВНИМАНИЕ: индекс построен с проекцией (models.embedder.projection_path), а проекция обучена на этих же фото — цифры "
    "ниже завышены и для оценки точности не годятся.** Честная проверка — `python -m ml.train_projection --check-only` "
    "(замер на товарах, которых проекция не видела).")


def projected_index(index_path: str) -> bool:
    """Индекс с векторами после проекции (паспорт модели с отметкой +proj-, app/pipeline/projection.py)."""
    import json  # noqa: PLC0415
    from app.pipeline.projection import PROJ_MARK  # noqa: PLC0415
    sidecar = Path(str(index_path) + ".ids.json")
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    model = ((raw.get("manifest") or {}).get("model") or raw.get("model")) if isinstance(raw, dict) else None
    return isinstance(model, dict) and PROJ_MARK in str(model.get("preprocess", ""))


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")         # консоль Windows (cp1251) не печатает «≥» и т. п.
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", default=None, help="индекс (по умолчанию models.matcher.index_path из config.yaml)")
    ap.add_argument("--out", default=str(PROJECT_ROOT / "data" / "benchmark" / "similar"))
    ap.add_argument("--copy", type=float, default=COPY_SIMILARITY)
    a = ap.parse_args(argv)
    from app.core.config import get_settings                         # noqa: PLC0415
    cfg = get_settings().matcher
    if cfg is None:
        print("В configs/config.yaml не задана секция models.matcher")
        return 2
    index_path = a.index or str(PROJECT_ROOT / cfg.index_path)
    vectors, owners, front, items = _load_from_service(index_path)
    siblings = size_siblings(list(items.values()))
    families = brand_families(list(items.values()))
    queries = leave_one_out(vectors, owners, a.copy)
    strangers = out_of_catalog(vectors, owners)
    summary = summarize(queries, items, front, siblings, families)
    grid = decision_grid(queries, strangers, front, siblings, cfg.unknown_threshold)
    text = render(summary, grid, (cfg.match_threshold, cfg.margin_threshold), cfg.unknown_threshold, len(owners),
                  len(items), siblings, items)
    if projected_index(index_path):
        text = PROJECTION_WARNING + "\n\n" + text
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(text, encoding="utf-8")
    with (out / "grid.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["match", "margin", "confident", "wrong", "wrong_size", "front_confident", "front_wrong", "stranger_accepted", "n"])
        for p in grid:
            w.writerow([p.match, p.margin, p.confident, p.wrong, p.wrong_size, p.front_confident, p.front_wrong,
                        round(p.stranger_accepted, 4), p.n])
    print(text)
    print(f"Отчёт: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
