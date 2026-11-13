"""
Набор для детектора на ВАШИХ фото полки: проверить и разбить датасет, предразметить рамки, измерить качество, дообучить.

    python -m ml.detector_kit preannotate --images photos/ --out photos/        # черновая разметка, чтобы не рисовать всё с нуля
    python -m ml.detector_kit split       --src photos/ --dst dataset/          # train/val + data.yaml
    python -m ml.detector_kit check       --dataset dataset/                     # формат, рамки, размер — до обучения
    python -m ml.detector_kit train       --data dataset/data.yaml               # дообучение + копирование весов в проект
    python -m ml.detector_kit evaluate    --images dataset/images/val --labels dataset/labels/val --out eval/

Полный порядок действий и советы по съёмке/разметке — docs/detector_quickstart.md.

Формат разметки — YOLO: на каждое фото `имя.txt`, в каждой строке `0 cx cy w h` (всё от 0 до 1 относительно размеров кадра);
класс один (0 = product — упаковка товара лицевой стороной). Это формат, который экспортируют LabelImg, CVAT и Roboflow.

Качество считается как точность/полнота/F1 при IoU >= 0.5 (рамка совпала с размеченной хотя бы наполовину по площади
пересечения/объединения). Это проще для понимания, чем mAP, и достаточно, чтобы понять «годится ли детектор для демонстрации».
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
DEFAULT_WEIGHTS_OUT = "models/detector/sku110k_yolo11n.pt"
MIN_TRAIN_IMAGES_WARN = 30
MIN_TRAIN_BOXES_WARN = 200
SMALL_OBJECT_REL_HEIGHT = 0.02     # медианная высота рамки меньше 2% кадра — объекты мелкие


class DatasetError(RuntimeError):
    pass


# --------------------------------------------------------------------------- формат YOLO и геометрия


@dataclass(frozen=True)
class YoloBox:
    cx: float
    cy: float
    w: float
    h: float
    cls: int = 0


def parse_label_text(text: str, n_classes: int = 1) -> tuple[list[YoloBox], list[str]]:
    """Разбор .txt разметки одного фото -> (рамки, ошибки с номерами строк). Пустой файл — допустим (фото без товаров)."""
    boxes: list[YoloBox] = []
    errors: list[str] = []
    for no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 5:
            errors.append(f"строка {no}: ожидалось 5 чисел (класс cx cy w h), найдено {len(parts)}"
                          + (" — похоже на формат с полигонами/ключевыми точками" if len(parts) > 5 else ""))
            continue
        try:
            cls = int(parts[0])
            cx, cy, w, h = (float(p) for p in parts[1:])
        except ValueError:
            errors.append(f"строка {no}: не числа ({line[:40]})")
            continue
        if not 0 <= cls < n_classes:
            errors.append(f"строка {no}: класс {cls} вне диапазона 0..{n_classes - 1}")
        elif w <= 0 or h <= 0:
            errors.append(f"строка {no}: нулевая или отрицательная ширина/высота")
        elif not all(0.0 <= v <= 1.0 for v in (cx, cy, w, h)):
            errors.append(f"строка {no}: значения должны быть от 0 до 1 (похоже, координаты в пикселях, а не нормированные)")
        elif cx - w / 2 < -1e-3 or cx + w / 2 > 1 + 1e-3 or cy - h / 2 < -1e-3 or cy + h / 2 > 1 + 1e-3:
            errors.append(f"строка {no}: рамка выходит за границы кадра")
        else:
            boxes.append(YoloBox(cx, cy, w, h, cls))
    return boxes, errors


def boxes_to_yolo_lines(boxes_xyxy: Iterable[tuple[float, float, float, float]], width: int, height: int, cls: int = 0) -> list[str]:
    """Пиксельные рамки (x1,y1,x2,y2) -> строки YOLO. Рамка обрезается по кадру; вырожденные (почти нулевые) пропускаются."""
    lines = []
    for x1, y1, x2, y2 in boxes_xyxy:
        x1, x2 = max(0.0, min(x1, x2)), min(float(width), max(x1, x2))
        y1, y2 = max(0.0, min(y1, y2)), min(float(height), max(y1, y2))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        lines.append(f"{cls} {(x1 + x2) / 2 / width:.6f} {(y1 + y2) / 2 / height:.6f} {(x2 - x1) / width:.6f} {(y2 - y1) / height:.6f}")
    return lines


def yolo_to_xyxy(b: YoloBox, width: int, height: int) -> tuple[float, float, float, float]:
    return ((b.cx - b.w / 2) * width, (b.cy - b.h / 2) * height, (b.cx + b.w / 2) * width, (b.cy + b.h / 2) * height)


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _images_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS) if folder.is_dir() else []


# --------------------------------------------------------------------------- проверка датасета


@dataclass
class DatasetReport:
    images: dict[str, int] = field(default_factory=dict)       # split -> число фото
    boxes: dict[str, int] = field(default_factory=dict)        # split -> число рамок
    unlabeled: dict[str, int] = field(default_factory=dict)    # split -> фото без/с пустой разметкой
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    median_box_height: float | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def check_dataset(root: Path, n_classes: int = 1, max_listed: int = 8) -> DatasetReport:
    """Проверяет датасет в раскладке root/images/{train,val} + root/labels/{train,val}."""
    rep = DatasetReport()
    heights: list[float] = []
    stems_by_split: dict[str, set[str]] = {}

    for split in ("train", "val"):
        img_dir, lbl_dir = root / "images" / split, root / "labels" / split
        images = _images_in(img_dir)
        rep.images[split] = len(images)
        rep.boxes[split] = 0
        rep.unlabeled[split] = 0
        stems_by_split[split] = {p.stem for p in images}
        if not img_dir.is_dir():
            rep.errors.append(f"нет папки {img_dir}")
            continue
        if not images:
            rep.errors.append(f"в {img_dir} нет изображений ({', '.join(IMAGE_EXTS)})")
            continue

        label_errors: list[str] = []
        bad_images: list[str] = []
        for p in images:
            try:
                from PIL import Image
                with Image.open(p) as im:  # exif-ok: только проверка целостности файла, пиксели не используются
                    im.verify()
            except Exception:    # noqa: BLE001 — любое исключение декодера = файл повреждён
                bad_images.append(p.name)
                continue
            lbl = lbl_dir / f"{p.stem}.txt"
            if not lbl.is_file():
                rep.unlabeled[split] += 1
                continue
            boxes, errs = parse_label_text(lbl.read_text(encoding="utf-8", errors="replace"), n_classes)
            label_errors += [f"{split}/{lbl.name}: {e}" for e in errs]
            if not boxes and not errs:
                rep.unlabeled[split] += 1
            rep.boxes[split] += len(boxes)
            heights += [b.h for b in boxes]
        if bad_images:
            rep.errors.append(f"повреждённые изображения в {split}: {', '.join(bad_images[:max_listed])}"
                              + (f" и ещё {len(bad_images) - max_listed}" if len(bad_images) > max_listed else ""))
        if label_errors:
            rep.errors += label_errors[:max_listed]
            if len(label_errors) > max_listed:
                rep.errors.append(f"…и ещё {len(label_errors) - max_listed} ошибок разметки")
        orphans = sorted(q.stem for q in lbl_dir.glob("*.txt") if q.stem not in stems_by_split[split]) if lbl_dir.is_dir() else []
        if orphans:
            rep.warnings.append(f"разметка без фото в {split}: {', '.join(orphans[:max_listed])}")
        if rep.unlabeled[split] and rep.unlabeled[split] / len(images) > 0.3:
            rep.warnings.append(f"в {split} {rep.unlabeled[split]} из {len(images)} фото без рамок — они считаются фоном; "
                                f"если это ошибка (не размечено), детектор научится игнорировать товары")

    overlap = stems_by_split.get("train", set()) & stems_by_split.get("val", set())
    if overlap:
        rep.warnings.append(f"одинаковые имена файлов в train и val ({', '.join(sorted(overlap)[:max_listed])}) — "
                            f"оценка будет завышена, если это одни и те же снимки")

    if rep.images.get("train", 0) and rep.images["train"] < MIN_TRAIN_IMAGES_WARN:
        rep.warnings.append(f"мало обучающих фото: {rep.images['train']} (рекомендуется от {MIN_TRAIN_IMAGES_WARN}; "
                            f"для одной и той же полки можно и 20–30, но качество будет хуже на других ракурсах)")
    if rep.boxes.get("train", 0) and rep.boxes["train"] < MIN_TRAIN_BOXES_WARN:
        rep.warnings.append(f"мало размеченных рамок для обучения: {rep.boxes['train']} (рекомендуется от {MIN_TRAIN_BOXES_WARN})")
    if heights:
        rep.median_box_height = _median(heights)
        if rep.median_box_height < SMALL_OBJECT_REL_HEIGHT:
            rep.warnings.append(f"объекты мелкие: медианная высота рамки {rep.median_box_height:.1%} кадра — снимайте ближе "
                                f"или обучайте с большим imgsz")
    return rep


def format_dataset_report(rep: DatasetReport) -> str:
    lines = []
    for split in ("train", "val"):
        if split in rep.images:
            lines.append(f"{split}: {rep.images[split]} фото, {rep.boxes.get(split, 0)} рамок, без разметки: {rep.unlabeled.get(split, 0)}")
    if rep.median_box_height is not None:
        lines.append(f"медианная высота рамки: {rep.median_box_height:.1%} кадра")
    lines += [f"ОШИБКА: {e}" for e in rep.errors] + [f"Предупреждение: {w}" for w in rep.warnings]
    lines.append("Датасет годится для обучения." if rep.ok else "Датасет НЕ годится: исправьте ошибки выше.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- разбиение на train/val


@dataclass
class SplitResult:
    train: int
    val: int
    skipped_unlabeled: int
    data_yaml: Path


def _find_label(img: Path, labels_dir: Path | None, src: Path) -> Path | None:
    for cand in ([labels_dir / f"{img.stem}.txt"] if labels_dir else []) + [img.with_suffix(".txt"), src / "labels" / f"{img.stem}.txt"]:
        if cand.is_file():
            return cand
    return None


def split_dataset(src: Path, dst: Path, *, val_ratio: float = 0.2, seed: int = 0, include_unlabeled: bool = False,
                  labels_dir: Path | None = None) -> SplitResult:
    """Копирует фото+разметку из плоской папки в раскладку YOLO (train/val) и пишет data.yaml. Исходные файлы не трогает."""
    if not 0 < val_ratio < 1:
        raise DatasetError("val_ratio должен быть между 0 и 1")
    if dst.exists() and any(dst.iterdir()):
        raise DatasetError(f"папка {dst} не пуста — укажите новую, чтобы не смешать старый и новый датасет")
    images = [p for p in sorted(src.rglob("*")) if p.is_file() and p.suffix.lower() in IMAGE_EXTS and dst.resolve() not in p.resolve().parents]
    stems = [p.stem for p in images]
    dupes = sorted({s for s in stems if stems.count(s) > 1})
    if dupes:
        raise DatasetError(f"повторяющиеся имена файлов (разные папки/расширения): {', '.join(dupes[:5])} — переименуйте")

    items, skipped = [], 0
    for img in images:
        lbl = _find_label(img, labels_dir, src)
        if lbl is None and not include_unlabeled:
            skipped += 1
            continue
        items.append((img, lbl))
    if len(items) < 2:
        raise DatasetError(f"для разбиения нужно минимум 2 размеченных фото, найдено {len(items)} "
                           f"(без разметки пропущено: {skipped}; флаг --include-unlabeled добавит их как фон)")

    rng = random.Random(seed)
    rng.shuffle(items)
    n_val = min(len(items) - 1, max(1, round(len(items) * val_ratio)))
    parts = {"val": items[:n_val], "train": items[n_val:]}
    for split, rows in parts.items():
        (dst / "images" / split).mkdir(parents=True, exist_ok=True)
        (dst / "labels" / split).mkdir(parents=True, exist_ok=True)
        for img, lbl in rows:
            shutil.copy2(img, dst / "images" / split / img.name)
            target = dst / "labels" / split / f"{img.stem}.txt"
            if lbl is not None:
                shutil.copy2(lbl, target)
            else:
                target.write_text("", encoding="utf-8")
    yaml_path = dst / "data.yaml"
    yaml_path.write_text(f"path: {dst.resolve().as_posix()}\ntrain: images/train\nval: images/val\nnames:\n  0: product\n", encoding="utf-8")
    return SplitResult(len(parts["train"]), len(parts["val"]), skipped, yaml_path)


# --------------------------------------------------------------------------- предразметка


@dataclass
class PreannotateResult:
    written: int
    skipped_existing: int
    boxes: int


def load_detector(weights_path: str | None):
    """-> (детектор, описание). Нет весов — эвристика по контурам (грубая, но хоть что-то для черновой разметки)."""
    from app.pipeline.detector import HeuristicContourDetector, YoloShelfDetector
    if weights_path and Path(weights_path).is_file():
        return YoloShelfDetector(weights_path), f"YOLO ({weights_path})"
    return HeuristicContourDetector(), "эвристика по контурам (весов нет — рамок будет много лишних/склеенных, правьте вручную)"


def _read_rgb(path: Path):
    from app.pipeline.imageio import load_rgb      # с учётом EXIF-поворота — как видит снимок редактор разметки
    return load_rgb(path)


def preannotate(images_dir: Path, out_dir: Path, detector, *, overwrite: bool = False) -> PreannotateResult:
    """Пишет черновые YOLO-файлы по результатам детектора. Существующую (ручную!) разметку НЕ трогает без overwrite."""
    out_dir.mkdir(parents=True, exist_ok=True)
    classes = out_dir / "classes.txt"                  # его читает LabelImg, чтобы показать название класса
    if not classes.exists():
        classes.write_text("product\n", encoding="utf-8")
    written = skipped = total = 0
    for img in _images_in(images_dir):
        target = out_dir / f"{img.stem}.txt"
        if target.exists() and not overwrite:
            skipped += 1
            continue
        rgb = _read_rgb(img)
        h, w = rgb.shape[:2]
        lines = boxes_to_yolo_lines([(d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2) for d in detector.detect(rgb)], w, h)
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        written += 1
        total += len(lines)
    return PreannotateResult(written, skipped, total)


# --------------------------------------------------------------------------- оценка качества


def match_boxes(pred: list[tuple[tuple[float, float, float, float], float]], truth: list[tuple[float, float, float, float]],
                iou_thr: float = 0.5) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Жадное сопоставление: предсказания по убыванию уверенности, каждая размеченная рамка занимается один раз.
    -> (пары (pred_idx, truth_idx), индексы ложных предсказаний, индексы пропущенных размеченных)."""
    order = sorted(range(len(pred)), key=lambda i: pred[i][1], reverse=True)
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    fp: list[int] = []
    for i in order:
        best, best_iou = -1, iou_thr
        for j, t in enumerate(truth):
            if j in used:
                continue
            v = iou(pred[i][0], t)
            if v >= best_iou:
                best, best_iou = j, v
        if best >= 0:
            used.add(best)
            pairs.append((i, best))
        else:
            fp.append(i)
    return pairs, fp, [j for j in range(len(truth)) if j not in used]


@dataclass
class ImageEval:
    name: str
    n_true: int
    n_pred: int
    tp: int
    fp: int
    fn: int


@dataclass
class EvalReport:
    per_image: list[ImageEval]
    iou_thr: float
    mean_pred_to_true_area: float | None = None

    @property
    def tp(self) -> int:
        return sum(e.tp for e in self.per_image)

    @property
    def fp(self) -> int:
        return sum(e.fp for e in self.per_image)

    @property
    def fn(self) -> int:
        return sum(e.fn for e in self.per_image)

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def draw_overlay(rgb, pred_xyxy, truth_xyxy, pairs, fp, fn):
    """Зелёный — найденный товар, красный — лишняя рамка детектора, синий — пропущенный размеченный товар."""
    import cv2
    img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()
    t = max(1, round(min(img.shape[:2]) / 400))
    for i, _ in pairs:
        x1, y1, x2, y2 = (int(v) for v in pred_xyxy[i])
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), t)
    for i in fp:
        x1, y1, x2, y2 = (int(v) for v in pred_xyxy[i])
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 230), t)
    for j in fn:
        x1, y1, x2, y2 = (int(v) for v in truth_xyxy[j])
        cv2.rectangle(img, (x1, y1), (x2, y2), (230, 90, 0), t)
    cv2.putText(img, f"found {len(pairs)}  extra {len(fp)}  missed {len(fn)}", (8, 26 * t), cv2.FONT_HERSHEY_SIMPLEX,
                0.5 * t, (255, 255, 255), t + 1)
    cv2.putText(img, f"found {len(pairs)}  extra {len(fp)}  missed {len(fn)}", (8, 26 * t), cv2.FONT_HERSHEY_SIMPLEX,
                0.5 * t, (0, 0, 0), t)
    return img


def evaluate_folder(images_dir: Path, labels_dir: Path, detector, *, iou_thr: float = 0.5, overlay_dir: Path | None = None) -> EvalReport:
    import cv2
    per_image: list[ImageEval] = []
    ratios: list[float] = []
    if overlay_dir:
        overlay_dir.mkdir(parents=True, exist_ok=True)
    for img in _images_in(images_dir):
        lbl = labels_dir / f"{img.stem}.txt"
        if not lbl.is_file():
            continue                                     # без разметки оценивать нечем
        rgb = _read_rgb(img)
        h, w = rgb.shape[:2]
        boxes, errs = parse_label_text(lbl.read_text(encoding="utf-8", errors="replace"))
        if errs:
            raise DatasetError(f"{lbl.name}: {errs[0]}")
        truth = [yolo_to_xyxy(b, w, h) for b in boxes]
        dets = detector.detect(rgb)
        pred = [((d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2), d.confidence) for d in dets]
        pairs, fp, fn = match_boxes(pred, truth, iou_thr)
        per_image.append(ImageEval(img.name, len(truth), len(pred), len(pairs), len(fp), len(fn)))
        area = lambda b: max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])   # noqa: E731
        if truth and pred:
            ratios.append((sum(area(p[0]) for p in pred) / len(pred)) / (sum(area(t) for t in truth) / len(truth)))
        if overlay_dir:
            cv2.imwrite(str(overlay_dir / f"{img.stem}.jpg"), draw_overlay(rgb, [p[0] for p in pred], truth, pairs, fp, fn))
    if not per_image:
        raise DatasetError(f"нет ни одной пары «фото + разметка» (фото: {images_dir}, разметка: {labels_dir})")
    return EvalReport(per_image, iou_thr, sum(ratios) / len(ratios) if ratios else None)


def diagnose(rep: EvalReport) -> list[str]:
    """Что делать по результатам оценки — простыми словами."""
    tips: list[str] = []
    n_true = sum(e.n_true for e in rep.per_image)
    n_pred = sum(e.n_pred for e in rep.per_image)
    if rep.f1 >= 0.85:
        tips.append("Результат хороший для демонстрации на этой полке.")
    elif rep.f1 >= 0.6:
        tips.append("Результат средний: часть товаров детектор ещё путает или пропускает — добавьте фото и разметку, обучите заново.")
    else:
        tips.append("Результат слабый: для демонстрации так использовать нельзя — нужны веса, обученные на ваших фото (docs/detector_quickstart.md).")
    if rep.mean_pred_to_true_area is not None and rep.mean_pred_to_true_area > 3.0:
        tips.append(f"Рамки детектора в среднем в {rep.mean_pred_to_true_area:.0f} раз больше размеченных: соседние товары склеиваются "
                    f"в одну рамку (типично для эвристики по контурам, не для обученного YOLO).")
    if n_true and n_pred > 2 * n_true:
        tips.append(f"Детектор нашёл {n_pred} рамок при {n_true} размеченных: много лишних — добавьте фото-фоны (полка без товара), "
                    f"проверьте, что на фото размечены ВСЕ товары.")
    if n_true and rep.recall < 0.7 and n_pred < n_true:
        tips.append("Детектор пропускает много товаров: больше фото с разных ракурсов и при разном свете, дольше обучение.")
    return tips


def format_eval_report(rep: EvalReport) -> str:
    lines = [f"Фото: {len(rep.per_image)}   IoU >= {rep.iou_thr}",
             f"Найдено верно (TP): {rep.tp}   лишних (FP): {rep.fp}   пропущено (FN): {rep.fn}",
             f"Точность: {rep.precision:.1%}   Полнота: {rep.recall:.1%}   F1: {rep.f1:.1%}"]
    worst = sorted(rep.per_image, key=lambda e: e.fp + e.fn, reverse=True)[:3]
    if worst and worst[0].fp + worst[0].fn:
        lines.append("Хуже всего: " + ", ".join(f"{e.name} (лишних {e.fp}, пропущено {e.fn})" for e in worst))
    lines += ["", *diagnose(rep)]
    return "\n".join(lines)


# --------------------------------------------------------------------------- обучение


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except ImportError:
        return False


def train(data_yaml: Path, *, base: str = "yolov8s.pt", epochs: int = 80, imgsz: int = 1280, batch: int | None = None,
          device=None, out_weights: str = DEFAULT_WEIGHTS_OUT, project: str = "runs/detect", name: str = "shelf",
          yolo_factory: Callable | None = None, cuda_available: Callable[[], bool] = _cuda_available,
          out=print) -> Path:
    """Дообучает YOLO на вашем датасете и копирует лучшие веса туда, откуда их берёт проект (прежние — в .bak)."""
    data_yaml = Path(data_yaml)
    if not data_yaml.is_file():
        raise DatasetError(f"нет файла {data_yaml} (создайте командой split)")
    rep = check_dataset(data_yaml.parent)
    if not rep.ok:
        raise DatasetError("датасет не прошёл проверку:\n" + format_dataset_report(rep))
    for w in rep.warnings:
        out(f"Предупреждение: {w}")

    cuda = cuda_available()
    device = device if device is not None else (0 if cuda else "cpu")
    batch = batch if batch is not None else (-1 if cuda else 4)        # -1 = автоподбор под память видеокарты
    if device == "cpu":
        out("ВНИМАНИЕ: обучение на процессоре очень медленное (часы). Проверьте драйвер и PyTorch с CUDA: python -m scripts.preflight")

    if yolo_factory is None:
        from ultralytics import YOLO as yolo_factory   # noqa: N811 — лениво: тяжёлый импорт нужен только для обучения
    model = yolo_factory(base)
    out(f"Обучение: base={base} epochs={epochs} imgsz={imgsz} batch={batch} device={device}")
    results = model.train(data=str(data_yaml), epochs=epochs, imgsz=imgsz, batch=batch, device=device, project=project, name=name,
                          exist_ok=True, patience=25, single_cls=True, workers=2)

    candidates = [getattr(results, "save_dir", None), getattr(getattr(model, "trainer", None), "save_dir", None), Path(project) / name]
    best = next((Path(c) / "weights" / "best.pt" for c in candidates if c and (Path(c) / "weights" / "best.pt").is_file()), None)
    if best is None:
        raise DatasetError("обучение завершилось, но weights/best.pt не найден — смотрите вывод обучения выше")

    target = Path(out_weights)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(f"{target.stem}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
        shutil.copy2(target, backup)
        out(f"Прежние веса сохранены: {backup}")
    shutil.copy2(best, target)
    out(f"Готово: веса скопированы в {target}. Проверьте качество: python -m ml.detector_kit evaluate ...")
    return target


# --------------------------------------------------------------------------- командная строка


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preannotate", help="черновая разметка детектором (классы.txt для LabelImg создаётся)")
    p.add_argument("--images", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--weights", default=DEFAULT_WEIGHTS_OUT, help="веса YOLO; нет файла — эвристика")
    p.add_argument("--overwrite", action="store_true", help="перезаписать существующую разметку (по умолчанию не трогаем)")

    p = sub.add_parser("split", help="разложить фото+разметку на train/val и создать data.yaml")
    p.add_argument("--src", required=True)
    p.add_argument("--dst", required=True)
    p.add_argument("--labels", help="папка с .txt, если они не рядом с фото")
    p.add_argument("--val", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--include-unlabeled", action="store_true")

    p = sub.add_parser("check", help="проверить датасет до обучения")
    p.add_argument("--dataset", required=True)

    p = sub.add_parser("train", help="дообучить YOLO и скопировать веса в проект")
    p.add_argument("--data", required=True)
    p.add_argument("--base", default="yolov8s.pt")
    p.add_argument("--epochs", type=int, default=80)
    p.add_argument("--imgsz", type=int, default=1280)
    p.add_argument("--batch", type=int)
    p.add_argument("--out-weights", default=DEFAULT_WEIGHTS_OUT)

    p = sub.add_parser("evaluate", help="точность/полнота детектора на размеченных фото + картинки с рамками")
    p.add_argument("--images", required=True)
    p.add_argument("--labels", required=True)
    p.add_argument("--weights", default=DEFAULT_WEIGHTS_OUT)
    p.add_argument("--out", help="папка для картинок: зелёный — найдено, красный — лишнее, синий — пропущено")
    p.add_argument("--iou", type=float, default=0.5)
    return ap


def main(argv: list[str] | None = None, out=print) -> int:
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "preannotate":
            detector, desc = load_detector(a.weights)
            out(f"Детектор: {desc}")
            r = preannotate(Path(a.images), Path(a.out), detector, overwrite=a.overwrite)
            out(f"Записано файлов разметки: {r.written} (рамок: {r.boxes}), пропущено существующих: {r.skipped_existing}")
            out("Теперь откройте фото в LabelImg/CVAT, исправьте рамки (удалите лишние, добавьте пропущенные).")
        elif a.cmd == "split":
            r = split_dataset(Path(a.src), Path(a.dst), val_ratio=a.val, seed=a.seed, include_unlabeled=a.include_unlabeled,
                              labels_dir=Path(a.labels) if a.labels else None)
            out(f"train: {r.train}, val: {r.val}, пропущено без разметки: {r.skipped_unlabeled}\nСоздан {r.data_yaml}")
        elif a.cmd == "check":
            rep = check_dataset(Path(a.dataset))
            out(format_dataset_report(rep))
            return 0 if rep.ok else 1
        elif a.cmd == "train":
            train(Path(a.data), base=a.base, epochs=a.epochs, imgsz=a.imgsz, batch=a.batch, out_weights=a.out_weights, out=out)
        elif a.cmd == "evaluate":
            detector, desc = load_detector(a.weights)
            out(f"Детектор: {desc}")
            rep = evaluate_folder(Path(a.images), Path(a.labels), detector, iou_thr=a.iou, overlay_dir=Path(a.out) if a.out else None)
            out(format_eval_report(rep))
            if a.out:
                out(f"Картинки с рамками: {a.out}")
                (Path(a.out) / "report.json").write_text(json.dumps(
                    {"precision": rep.precision, "recall": rep.recall, "f1": rep.f1, "tp": rep.tp, "fp": rep.fp, "fn": rep.fn,
                     "per_image": [e.__dict__ for e in rep.per_image]}, ensure_ascii=False, indent=2), encoding="utf-8")
    except DatasetError as exc:
        out(f"Ошибка: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
