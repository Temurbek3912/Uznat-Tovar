"""
Быстрый «взгляд» на детектор: видит ли обученный YOLO товар на ВАШИХ фото. Без разметки; в приложении ничего не меняет.

    python -m ml.probe_detector --photos data/probe --out data/probe_out

Зачем. YOLO обучен на фото полок с десятками мелких товаров (SKU-110K). Товар, который человек держит перед камерой, выглядит
иначе, и заранее неизвестно, увидит ли его детектор. Этот скрипт показывает это на ваших фото, прежде чем мы строим на детекторе
распознавание.

Для каждого фото считает: сколько рамок найдено при рабочем пороге (config.yaml, 0.35) и сколько «слабых» (от 0.10 до рабочего;
диагностика), насколько уверена самая сильная рамка, какую долю кадра занимает самая крупная. Заодно проверяет порядок цветов:
боевой код отдаёт YOLO картинку в порядке RGB, а Ultralytics для массивов numpy ждёт BGR (как у OpenCV) — скрипт запускает детектор
в обоих вариантах и показывает, где результат различается.

Результат: таблица на экране, `report.csv` (открывается в Excel) и копии фото с нарисованными рамками (зелёная — рабочий порог,
оранжевая — слабая). Исходные фото не изменяются.

`--heuristic` — только для проверки самого скрипта (простой детектор контуров вместо вашей модели); к вашей модели отношения не имеет.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEIGHTS = PROJECT_ROOT / "models" / "detector" / "sku110k_yolo11n.pt"
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
BGR_MARGIN = 0.05          # разницу в уверенности меньше этого считаем «одинаково»

Box = tuple[float, float, float, float, float]      # x1, y1, x2, y2, уверенность

ONE, SEVERAL, WEAK, NONE = "один товар", "несколько", "слабо", "не найден"


def verdict_of(strong: int, weak: int) -> str:
    if strong == 1:
        return ONE
    if strong > 1:
        return SEVERAL
    return WEAK if weak > 0 else NONE


def summarize(boxes: Sequence[Box], shape: tuple[int, ...], conf: float, low_conf: float) -> dict:
    """boxes — всё, что нашёл детектор при НИЗКОМ пороге; рабочий порог conf применяем здесь."""
    height, width = shape[0], shape[1]
    strong = [b for b in boxes if b[4] >= conf]
    weak = [b for b in boxes if low_conf <= b[4] < conf]
    pool = strong or list(boxes)
    biggest = max(pool, key=lambda b: max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1]), default=None)
    share = 0.0
    if biggest is not None and width > 0 and height > 0:
        share = max(0.0, biggest[2] - biggest[0]) * max(0.0, biggest[3] - biggest[1]) / float(width * height)
    return {
        "strong": len(strong),
        "weak": len(weak),
        "top": max((b[4] for b in boxes), default=0.0),
        "share": share,
        "verdict": verdict_of(len(strong), len(weak)),
    }


def compare_rgb_bgr(top_rgb: float, top_bgr: float, margin: float = BGR_MARGIN) -> str:
    if top_bgr - top_rgb >= margin:
        return "BGR лучше"
    if top_rgb - top_bgr >= margin:
        return "RGB лучше"
    return "одинаково"


def _boxes(detections) -> list[Box]:
    return [(float(d.bbox.x1), float(d.bbox.y1), float(d.bbox.x2), float(d.bbox.y2), float(d.confidence)) for d in detections]


def draw_boxes(image: np.ndarray, boxes: Sequence[Box], conf: float, path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    picture = Image.fromarray(image)
    pen = ImageDraw.Draw(picture)
    line = max(2, round(max(image.shape[:2]) / 300))
    try:
        font = ImageFont.load_default(size=max(12, line * 6))
    except TypeError:                                 # старая Pillow: шрифт без размера
        font = ImageFont.load_default()
    for x1, y1, x2, y2, score in boxes:
        color = (0, 200, 0) if score >= conf else (255, 165, 0)
        pen.rectangle([x1, y1, x2, y2], outline=(0, 0, 0), width=line + 2)          # тёмная подложка: рамка видна на любом товаре
        pen.rectangle([x1, y1, x2, y2], outline=color, width=line)
        label = f"{score:.2f}"
        left, top, right, bottom = pen.textbbox((x1, y1), label, font=font)
        pen.rectangle([left - 2, top - 2, right + 2, bottom + 2], fill=color)       # подпись на цветной плашке
        pen.text((x1, y1), label, fill=(0, 0, 0), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    picture.save(path, quality=90)


def probe_folder(photos: Sequence[Path], detector, out_dir: Path, conf: float, low_conf: float,
                 load: Callable[[Path], np.ndarray] | None = None) -> list[dict]:
    """Прогоняет детектор по фото; один нечитаемый файл не останавливает остальные. Исходные фото не меняются."""
    if load is None:
        from app.pipeline.imageio import load_rgb as load          # EXIF-поворот телефона учитывается так же, как в приложении
    rows: list[dict] = []
    for path in photos:
        row: dict = {"file": path.name, "error": ""}
        try:
            image = load(path)
            rgb = _boxes(detector.detect(image))
            bgr = _boxes(detector.detect(np.ascontiguousarray(image[..., ::-1])))
            info = summarize(rgb, image.shape, conf, low_conf)
            other = summarize(bgr, image.shape, conf, low_conf)
            row.update(info)
            row.update({"bgr_strong": other["strong"], "bgr_top": other["top"], "colors": compare_rgb_bgr(info["top"], other["top"])})
            draw_boxes(image, rgb, conf, out_dir / f"{path.stem}__boxes.jpg")
        except Exception as exc:  # noqa: BLE001 — по одному фото нельзя ронять весь прогон
            row.update({"strong": 0, "weak": 0, "top": 0.0, "share": 0.0, "verdict": "ошибка", "bgr_strong": 0, "bgr_top": 0.0,
                        "colors": "", "error": f"{type(exc).__name__}: {exc}"})
        rows.append(row)
    return rows


COLUMNS = [("file", "Файл"), ("strong", "Рамок"), ("weak", "Слабых"), ("top", "Макс.увер."), ("share", "Доля кадра"),
           ("verdict", "Вердикт"), ("bgr_strong", "BGR: рамок"), ("bgr_top", "BGR: макс."), ("colors", "Цвета"), ("error", "Ошибка")]


def _cell(key: str, value) -> str:
    if key in ("top", "bgr_top"):
        return f"{value:.2f}"
    if key == "share":
        return f"{value * 100:.0f}%"
    return str(value)


def format_table(rows: Sequence[dict]) -> str:
    table = [[title for _, title in COLUMNS]] + [[_cell(key, row.get(key, "")) for key, _ in COLUMNS] for row in rows]
    widths = [min(34, max(len(line[i]) for line in table)) for i in range(len(COLUMNS))]
    out = []
    for n, line in enumerate(table):
        out.append("  ".join(cell[:widths[i]].ljust(widths[i]) for i, cell in enumerate(line)).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def format_summary(rows: Sequence[dict], conf: float) -> str:
    total = len(rows)
    count = lambda name: sum(1 for r in rows if r["verdict"] == name)       # noqa: E731
    colors = lambda name: sum(1 for r in rows if r.get("colors") == name)   # noqa: E731
    lines = [
        f"Итого фото: {total}. При рабочем пороге {conf:g}: один товар — {count(ONE)}, несколько — {count(SEVERAL)}, "
        f"только слабые рамки — {count(WEAK)}, не найдено ничего — {count(NONE)}, ошибки чтения — {count('ошибка')}.",
        f"Порядок цветов: BGR лучше на {colors('BGR лучше')}, RGB лучше на {colors('RGB лучше')}, одинаково на {colors('одинаково')}.",
        "Как читать: «один товар» на большинстве кадров — детектор годится для режима «покажи товар»; много «слабо» и «не найден» — "
        "для одиночного товара нужен запасной путь. Это измерение на ваших фото, а не оценка точности распознавания.",
    ]
    return "\n".join(lines)


def write_csv(rows: Sequence[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:          # utf-8-sig: Excel читает кириллицу
        writer = csv.writer(fh, delimiter=";")
        writer.writerow([title for _, title in COLUMNS])
        for row in rows:
            writer.writerow([_cell(key, row.get(key, "")) for key, _ in COLUMNS])


def build_detector(weights: Path, low_conf: float, heuristic: bool):
    if heuristic:
        from app.pipeline.detector import HeuristicContourDetector

        print("РЕЖИМ ПРОВЕРКИ СКРИПТА: используется простой детектор контуров, а не ваша модель YOLO.")
        return HeuristicContourDetector()
    if not weights.is_file():                                            # проверяем ДО импорта тяжёлых библиотек
        raise FileNotFoundError(
            f"Файл весов детектора не найден: {weights}\n"
            "Положите обученную модель (best.pt) в проект как models/detector/sku110k_yolo11n.pt и запустите снова."
        )
    from app.pipeline.detector import YoloShelfDetector

    return YoloShelfDetector(str(weights), confidence_threshold=low_conf)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Видит ли YOLO товар на ваших фото (ничего в приложении не меняет).")
    parser.add_argument("--photos", required=True, help="папка с фото (jpg, png, webp, bmp)")
    parser.add_argument("--out", default="data/probe_out", help="куда сложить отчёт и фото с рамками (по умолчанию data/probe_out)")
    parser.add_argument("--weights", default=str(DEFAULT_WEIGHTS), help="файл весов YOLO (по умолчанию models/detector/sku110k_yolo11n.pt)")
    parser.add_argument("--conf", type=float, default=0.35, help="рабочий порог уверенности (как в config.yaml)")
    parser.add_argument("--low-conf", type=float, default=0.10, help="низкий порог для диагностики «слабых» рамок")
    parser.add_argument("--heuristic", action="store_true", help="только проверка скрипта: простой детектор вместо модели")
    args = parser.parse_args(argv)

    photos_dir, out_dir = Path(args.photos), Path(args.out)
    if not photos_dir.is_dir():
        print(f"Папка с фото не найдена: {photos_dir}")
        return 2
    photos = sorted(p for p in photos_dir.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES)
    if not photos:
        print(f"В папке {photos_dir} нет фото (jpg, png, webp, bmp).")
        return 2
    if args.low_conf > args.conf:
        print("Низкий порог не может быть выше рабочего.")
        return 2
    try:
        detector = build_detector(Path(args.weights), args.low_conf, args.heuristic)
    except (FileNotFoundError, RuntimeError) as exc:
        print(exc)
        return 2

    print(f"Фото: {len(photos)}. Рабочий порог {args.conf:g}, диагностический {args.low_conf:g}. Это может занять время (первый запуск дольше).\n")
    rows = probe_folder(photos, detector, out_dir, args.conf, args.low_conf)
    print(format_table(rows))
    print()
    print(format_summary(rows, args.conf))
    write_csv(rows, out_dir / "report.csv")
    print(f"\nОтчёт: {out_dir / 'report.csv'}\nФото с рамками: {out_dir} (файлы *__boxes.jpg)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
