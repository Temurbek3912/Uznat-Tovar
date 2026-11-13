"""
Оценка распознавания ТОВАРОВ (какой это SKU) и подбор порогов по вашим размеченным фото полок.

    python -m ml.evaluate_matching --images shelf_photos --labels shelf_photos --classes classes.txt

Входные данные — те же фото, что для детектора, но размеченные по товарам: YOLO-файлы `имя.txt`, где номер класса — это строка в
`classes.txt` (один артикул на строку, артикулы такие же, как в каталоге). Товар, которого нет в каталоге, называйте в классах
`unknown` (или любым артикулом, отсутствующим в каталоге) — на них проверяется, не принимает ли система чужое за свой товар.
Рамки берутся ИЗ РАЗМЕТКИ («идеальный детектор»): так качество распознавания отделено от качества детекции (её меряет
`python -m ml.detector_kit evaluate`). Кроп режется той же функцией, что и в боевом пайплайне.

Что считает: точность первого кандидата (top-1) и попадание в тройку (top-3); таблицу «порог совпадения × запас» с долей
авто-принятых, точностью принятых и долей уверенных ОШИБОК; рекомендованную рабочую точку при заданной целевой точности;
главные путаницы «что приняли за что»; советы по порогу «неизвестно». Решение в каждой точке принимает ровно `decide_match` —
та же функция, что в боевом матчере, поэтому подобранные значения соответствуют реальному поведению.

Важно про смысл цифр: это оценка на ВАШИХ фото. Чем больше разных магазинов, освещения и ракурсов в разметке, тем больше ей можно
верить; на 30 рамках из одной полки цифры шумные (отчёт честно предупреждает).
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from app.core.schemas import DetectionStatus
from app.pipeline.matcher import decide_match

UNKNOWN_NAMES = {"unknown", "__unknown__", "неизвестно", "other", "прочее"}
MIN_SAMPLES_WARN = 100
MIN_ACCEPTED_WARN = 30
DEFAULT_TARGET_PRECISION = 0.98
DEFAULT_MATCH_GRID = tuple(round(0.30 + 0.01 * i, 2) for i in range(66))        # 0.30 .. 0.95
DEFAULT_MARGIN_GRID = (0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15)


@dataclass(frozen=True)
class Sample:
    """Одна размеченная рамка: что на самом деле (None — товара нет в каталоге) и кандидаты индекса, лучший первым."""
    truth_sku: str | None
    ranked: list[tuple[str, float]]                 # (product_id, лучший счёт по товару), по убыванию
    source: str = ""                                # «файл:номер рамки» — для разбора ошибок


@dataclass
class Point:
    match_threshold: float
    margin: float
    n: int = 0
    auto_correct: int = 0                           # принято автоматически и верно
    auto_wrong: int = 0                             # принято автоматически, но это ДРУГОЙ товар — самая дорогая ошибка
    unknown_accepted: int = 0                       # товара нет в каталоге, а система «узнала» его как свой
    review: int = 0                                 # отправлено человеку
    rejected_known: int = 0                         # товар из каталога признан «неизвестным»
    unknown_correct: int = 0                        # чужое верно признано неизвестным

    @property
    def accepted(self) -> int:
        return self.auto_correct + self.auto_wrong + self.unknown_accepted

    @property
    def auto_rate(self) -> float:
        return self.accepted / self.n if self.n else 0.0

    @property
    def precision(self) -> float:
        return self.auto_correct / self.accepted if self.accepted else 1.0

    @property
    def error_rate(self) -> float:
        """Доля ВСЕХ рамок, принятых автоматически и неверно (включая «чужое приняли за своё»)."""
        return (self.auto_wrong + self.unknown_accepted) / self.n if self.n else 0.0

    @property
    def review_rate(self) -> float:
        return self.review / self.n if self.n else 0.0


# --------------------------------------------------------------------------- ядро (чистое)


def evaluate_point(samples: Sequence[Sample], match_threshold: float, unknown_threshold: float, margin: float,
                   id_to_sku: dict[str, str]) -> Point:
    pt = Point(match_threshold, margin, n=len(samples))
    for s in samples:
        r = decide_match(s.ranked, match_threshold, unknown_threshold, margin)
        predicted = id_to_sku.get(r.product_id) if r.product_id else None
        if r.status == DetectionStatus.MATCHED:
            if s.truth_sku is None:
                pt.unknown_accepted += 1
            elif predicted == s.truth_sku:
                pt.auto_correct += 1
            else:
                pt.auto_wrong += 1
        elif r.status == DetectionStatus.UNCERTAIN:
            pt.review += 1
        elif s.truth_sku is None:
            pt.unknown_correct += 1
        else:
            pt.rejected_known += 1
    return pt


def sweep(samples: Sequence[Sample], id_to_sku: dict[str, str], unknown_threshold: float,
          match_grid: Sequence[float] = DEFAULT_MATCH_GRID, margin_grid: Sequence[float] = DEFAULT_MARGIN_GRID) -> list[Point]:
    return [evaluate_point(samples, t, unknown_threshold, m, id_to_sku) for m in margin_grid for t in match_grid if t >= unknown_threshold]


def recommend(points: Sequence[Point], target_precision: float = DEFAULT_TARGET_PRECISION) -> tuple[Point | None, bool]:
    """-> (точка, достигнута_ли_цель). Из точек с точностью принятых >= цели берём с максимальной долей авто-принятых (при равенстве —
    с меньшим запасом, т.е. проще). Если цели не достигает ни одна — точка с наибольшей точностью (и флаг False)."""
    usable = [p for p in points if p.accepted > 0]
    if not usable:
        return None, False
    ok = [p for p in usable if p.precision >= target_precision]
    if ok:
        return max(ok, key=lambda p: (p.auto_rate, -p.margin, -p.match_threshold)), True
    return max(usable, key=lambda p: (p.precision, p.auto_rate)), False


def top_k_accuracy(samples: Sequence[Sample], id_to_sku: dict[str, str], k: int = 1) -> float | None:
    known = [s for s in samples if s.truth_sku is not None]
    if not known:
        return None
    hits = sum(1 for s in known if any(id_to_sku.get(pid) == s.truth_sku for pid, _ in s.ranked[:k]))
    return hits / len(known)


def confusions(samples: Sequence[Sample], id_to_sku: dict[str, str], top: int = 10) -> list[tuple[str, str, int]]:
    """Что принимали за что при неверном первом кандидате: (истинный SKU, предсказанный SKU, сколько раз)."""
    counter: Counter[tuple[str, str]] = Counter()
    for s in samples:
        if s.truth_sku is None or not s.ranked:
            continue
        predicted = id_to_sku.get(s.ranked[0][0], "?")
        if predicted != s.truth_sku:
            counter[(s.truth_sku, predicted)] += 1
    return [(a, b, n) for (a, b), n in counter.most_common(top)]


def percentile(values: Sequence[float], q: float) -> float | None:
    return float(np.percentile(values, q)) if len(values) else None


def score_distributions(samples: Sequence[Sample], id_to_sku: dict[str, str]) -> dict[str, list[float]]:
    """Лучший счёт отдельно для: верных, ошибочных (известный товар, неверный первый кандидат), чужих (нет в каталоге)."""
    out = {"correct": [], "wrong": [], "unknown": []}
    for s in samples:
        best = s.ranked[0] if s.ranked else None
        score = best[1] if best else 0.0
        if s.truth_sku is None:
            out["unknown"].append(score)
        elif best and id_to_sku.get(best[0]) == s.truth_sku:
            out["correct"].append(score)
        else:
            out["wrong"].append(score)
    return out


# --------------------------------------------------------------------------- сбор выборки из фото


def read_classes(path: Path) -> list[str]:
    names = [ln.strip() for ln in path.read_text(encoding="utf-8-sig").splitlines()]
    return [n for n in names if n]


def collect_samples(images_dir: Path, labels_dir: Path, classes: list[str], sku_to_id: dict[str, str],
                    embed: Callable[[list[np.ndarray]], np.ndarray], rank: Callable[[np.ndarray], list[tuple[str, float]]],
                    batch: int = 32) -> tuple[list[Sample], list[str]]:
    """-> (выборка, предупреждения). Рамки — из разметки; класс не из каталога и «unknown» -> товар не из каталога."""
    from app.pipeline.imageio import crop_box, load_rgb
    from ml.detector_kit import IMAGE_EXTS, parse_label_text, yolo_to_xyxy

    warnings: list[str] = []
    pending: list[tuple[np.ndarray, str | None, str]] = []
    for img in sorted(p for p in images_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS):
        lbl = labels_dir / f"{img.stem}.txt"
        if not lbl.is_file():
            continue
        boxes, errs = parse_label_text(lbl.read_text(encoding="utf-8", errors="replace"), n_classes=len(classes))
        if errs:
            raise ValueError(f"{lbl.name}: {errs[0]}")
        rgb = load_rgb(img)
        h, w = rgb.shape[:2]
        for i, b in enumerate(boxes):
            name = classes[b.cls]
            x1, y1, x2, y2 = yolo_to_xyxy(b, w, h)
            crop = crop_box(rgb, x1, y1, x2, y2)
            if min(crop.shape[:2]) < 8:
                warnings.append(f"{img.name}#{i}: рамка слишком мелкая — пропущена")
                continue
            truth = None if name.lower() in UNKNOWN_NAMES or name not in sku_to_id else name
            if truth is None and name.lower() not in UNKNOWN_NAMES:
                warnings.append(f"артикул «{name}» из разметки не найден в каталоге — считается товаром НЕ из каталога")
            pending.append((crop, truth, f"{img.name}#{i}"))
    samples: list[Sample] = []
    for start in range(0, len(pending), batch):
        chunk = pending[start:start + batch]
        embs = np.atleast_2d(embed([c for c, _, _ in chunk]))
        for (_, truth, src), e in zip(chunk, embs):
            samples.append(Sample(truth, rank(e), src))
    return samples, sorted(set(warnings))


# --------------------------------------------------------------------------- отчёт


@dataclass
class Report:
    n: int
    n_known: int
    n_unknown: int
    top1: float | None
    top3: float | None
    current: Point
    recommended: Point | None
    target_met: bool
    target_precision: float
    confusions: list[tuple[str, str, int]]
    advice: list[str] = field(default_factory=list)
    points: list[Point] = field(default_factory=list)


def build_report(samples: Sequence[Sample], id_to_sku: dict[str, str], *, match_threshold: float, unknown_threshold: float,
                 margin: float, target_precision: float = DEFAULT_TARGET_PRECISION) -> Report:
    points = sweep(samples, id_to_sku, unknown_threshold)
    rec, met = recommend(points, target_precision)
    cur = evaluate_point(samples, match_threshold, unknown_threshold, margin, id_to_sku)
    n_known = sum(1 for s in samples if s.truth_sku is not None)
    rep = Report(len(samples), n_known, len(samples) - n_known, top_k_accuracy(samples, id_to_sku, 1), top_k_accuracy(samples, id_to_sku, 3),
                 cur, rec, met, target_precision, confusions(samples, id_to_sku), points=points)
    dist = score_distributions(samples, id_to_sku)
    if len(samples) < MIN_SAMPLES_WARN:
        rep.advice.append(f"Выборка мала ({len(samples)} рамок, рекомендуется от {MIN_SAMPLES_WARN}) — цифры шумные, не делайте по ним выводов "
                          f"о конкретных порогах до расширения разметки.")
    if rec and rec.accepted < MIN_ACCEPTED_WARN:
        rep.advice.append(f"Рекомендованная точка опирается всего на {rec.accepted} принятых рамок — ненадёжно.")
    if rec is None:
        rep.advice.append("Ни при каких порогах нет автоматически принятых товаров — проверьте каталог и разметку.")
    elif not met:
        rep.advice.append(f"Целевая точность {target_precision:.0%} недостижима: лучшая найденная — {rec.precision:.1%} при {rec.auto_rate:.0%} "
                          f"авто-принятых. Улучшайте каталог (больше эталонов у путаемых товаров) или оставьте больше в ручной проверке.")
    elif rec.margin > margin or rec.match_threshold != match_threshold:
        rep.advice.append(f"Рекомендуется: match_threshold: {rec.match_threshold}, margin_threshold: {rec.margin} "
                          f"(сейчас {match_threshold} / {margin}) — в configs/config.yaml, секция models.matcher.")
    unk = dist["unknown"]
    if len(unk) >= 10:
        p95 = percentile(unk, 95)
        rep.advice.append(f"Чужие товары ({len(unk)} рамок): лучший счёт до {p95:.2f} в 95% случаев — unknown_threshold стоит держать "
                          f"не ниже {p95:.2f}, иначе чужое будет попадать в ревью/принятие (сейчас {unknown_threshold}).")
    elif not unk:
        rep.advice.append("В разметке нет товаров «не из каталога» — порог неизвестного (unknown_threshold) не проверен.")
    if rep.confusions:
        rep.advice.append("Главные путаницы — добавьте таким товарам эталонные фото с различающими ракурсами (docs/catalog_guide.md).")
    return rep


def format_report(rep: Report) -> str:
    def fmt(p: Point) -> str:
        return (f"авто-принято {p.auto_rate:.0%}, точность принятых {p.precision:.1%}, уверенных ошибок {p.error_rate:.1%} от всех, "
                f"в ревью {p.review_rate:.0%}")
    lines = [f"Рамок: {rep.n} (из каталога {rep.n_known}, чужих {rep.n_unknown})"]
    if rep.top1 is not None:
        lines.append(f"Первый кандидат верен: {rep.top1:.1%}   верный среди трёх лучших: {rep.top3:.1%}")
    c = rep.current
    lines.append(f"\nТекущие пороги (match {c.match_threshold}, margin {c.margin}): {fmt(c)}")
    if rep.recommended:
        r = rep.recommended
        lines.append(f"Рекомендуемая точка (цель точности {rep.target_precision:.0%}{'' if rep.target_met else ' — НЕ достигнута'}): "
                     f"match {r.match_threshold}, margin {r.margin}: {fmt(r)}")
    if rep.confusions:
        lines.append("\nЧто с чем путают (истинный -> принятый, раз):")
        lines += [f"  {a} -> {b}: {n}" for a, b, n in rep.confusions]
    if rep.advice:
        lines += ["", *[f"* {a}" for a in rep.advice]]
    return "\n".join(lines)


def write_sweep_csv(points: Sequence[Point], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["match_threshold", "margin", "n", "auto_rate", "precision", "error_rate", "review_rate"])
        for p in points:
            w.writerow([p.match_threshold, p.margin, p.n, f"{p.auto_rate:.4f}", f"{p.precision:.4f}", f"{p.error_rate:.4f}", f"{p.review_rate:.4f}"])


# --------------------------------------------------------------------------- командная строка


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", required=True)
    ap.add_argument("--labels", required=True, help="папка с YOLO-разметкой по товарам (часто та же, что с фото)")
    ap.add_argument("--classes", help="classes.txt: артикул на строку; по умолчанию <labels>/classes.txt")
    ap.add_argument("--manifest", default="models/index/import_manifest.json", help="манифест импорта каталога (артикул -> product_id)")
    ap.add_argument("--target-precision", type=float, default=DEFAULT_TARGET_PRECISION)
    ap.add_argument("--out", help="папка для sweep.csv и report.json")
    return ap


def main(argv: list[str] | None = None, out=print) -> int:
    a = build_parser().parse_args(argv)
    images, labels = Path(a.images), Path(a.labels)
    classes_path = Path(a.classes) if a.classes else labels / "classes.txt"
    if not classes_path.is_file():
        out(f"Нет файла классов {classes_path}: нужен classes.txt (артикул на строку, как в каталоге).")
        return 2

    from app.core.config import get_settings                         # ленивые импорты: тяжёлые зависимости нужны только здесь
    from app.pipeline.embedder import DinoV2Embedder
    from app.pipeline.matcher import ProductMatcher
    from app.services.catalog_import import Manifest
    from scripts.add_product_from_local_file import _load_index

    settings = get_settings()
    if settings.embedder is None or settings.matcher is None:
        out("В configs/config.yaml не заданы models.embedder / models.matcher")
        return 2
    manifest = Manifest.load(Path(a.manifest))
    if not manifest.products:
        out(f"Манифест {a.manifest} пуст: загрузите каталог командой python -m scripts.import_catalog (артикулы берутся из него).")
        return 2
    id_to_sku = {pid: sku for sku, pid in manifest.products.items()}
    embedder = DinoV2Embedder(weights_path=settings.embedder.weights_path, embedding_dim=settings.embedder.embedding_dim)
    index = _load_index(settings.matcher.index_path, settings.embedder.embedding_dim)
    mcfg = settings.matcher
    matcher = ProductMatcher(index, top_k=mcfg.top_k, match_threshold=mcfg.match_threshold, unknown_threshold=mcfg.unknown_threshold,
                             margin_threshold=mcfg.margin_threshold)
    try:
        samples, warnings = collect_samples(images, labels, read_classes(classes_path), manifest.products, embedder.embed, matcher.rank)
    except ValueError as exc:
        out(f"Ошибка разметки: {exc}")
        return 1
    if not samples:
        out("Нет ни одной размеченной рамки (фото + .txt с рамками и classes.txt).")
        return 1
    for w in warnings[:20]:
        out(f"Предупреждение: {w}")
    rep = build_report(samples, id_to_sku, match_threshold=mcfg.match_threshold, unknown_threshold=mcfg.unknown_threshold,
                       margin=mcfg.margin_threshold, target_precision=a.target_precision)
    out(format_report(rep))
    if a.out:
        outdir = Path(a.out)
        outdir.mkdir(parents=True, exist_ok=True)
        write_sweep_csv(rep.points, outdir / "sweep.csv")
        (outdir / "report.json").write_text(json.dumps({
            "n": rep.n, "top1": rep.top1, "top3": rep.top3, "recommended": rep.recommended.__dict__ if rep.recommended else None,
            "target_met": rep.target_met, "confusions": rep.confusions, "advice": rep.advice}, ensure_ascii=False, indent=2), encoding="utf-8")
        out(f"\nТаблица порогов: {outdir / 'sweep.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
