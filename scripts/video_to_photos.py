"""
Короткое видео товара с телефона -> 10–15 чётких и разных фото в папке каталога (формат scripts/import_catalog.py).

Зачем: в открытых базах у товара 1–5 фото, а для уверенного распознавания нужно 10–15 с разных сторон (решение владельца
2026-11-17, docs/CATALOG_SOURCES.md). Снять 15 фото руками долго; видео 10–15 секунд по кругу — быстро.

Как снимать: товар на столе, хороший свет; телефон медленно обходит товар по кругу (перед, бок, зад, другой бок, немного
сверху); этикетка с объёмом («0,5 л», «1,5 л») должна попасть в кадр крупно хотя бы раз. Одно видео — один товар.

    # одно видео:
    python -m scripts.video_to_photos --video pepsi05.mp4 --sku PEPSI-0.5L --name "Pepsi 0,5 л" --category Напитки \\
        --barcode 4780022620153 --out D:/uznat_tovar/catalog-sets/phone
    # папка видео: имя файла = артикул (PEPSI-0.5L.mp4), названия — потом в products.csv или в программе:
    python -m scripts.video_to_photos --videos D:/videos --out D:/uznat_tovar/catalog-sets/phone
    # затем загрузка в программу и штрихкоды:
    python -m scripts.import_catalog --dir D:/uznat_tovar/catalog-sets/phone
    python -m scripts.fetch_open_catalog --link-barcodes --out D:/uznat_tovar/catalog-sets/phone

Видео не копируется в программу и не хранится — только выбранные кадры (JPG, длинная сторона до 1280 px).
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".webm")
DEFAULT_COUNT = 15
SAMPLE_FPS = 4.0            # сколько кадров в секунду смотреть (остальные — почти одинаковые соседи)
MAX_SIDE = 1280
BLUR_SHARE = 0.45           # кадр мутнее, чем 45 % от медианной чёткости видео, — смаз (телефон двигался)
MIN_DIFF = 6.0              # средняя разница уменьшенных кадров (0–255) — меньше: почти тот же вид, второй не нужен


@dataclass
class Frame:
    index: int              # номер кадра в видео
    sharpness: float        # дисперсия лапласиана: больше — чётче
    thumb: np.ndarray       # уменьшенный серый кадр 32×32 — сравнивать «тот же вид или другой»


def sharpness(gray: np.ndarray) -> float:
    import cv2  # noqa: PLC0415
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def pick_frames(frames: list[Frame], count: int = DEFAULT_COUNT) -> list[int]:
    """Номера кадров: без смазанных, равномерно по всему видео (разные стороны товара), без почти одинаковых.

    Видео делится на count равных отрезков; в каждом берётся самый чёткий кадр, если он не повторяет уже выбранный.
    Если отрезков с новым видом меньше — добираются самые чёткие из оставшихся разных."""
    if not frames or count <= 0:
        return []
    median = float(np.median([f.sharpness for f in frames]))
    good = [f for f in frames if f.sharpness >= median * BLUR_SHARE] or frames
    picked: list[Frame] = []

    def new_view(f: Frame) -> bool:
        return all(float(np.mean(np.abs(f.thumb.astype(np.float32) - p.thumb.astype(np.float32)))) >= MIN_DIFF for p in picked)

    size = max(1, len(good) // count) if len(good) >= count else 1
    for start in range(0, len(good), size):
        if len(picked) >= count:
            break
        best = max(good[start:start + size], key=lambda f: f.sharpness)
        if new_view(best):
            picked.append(best)
    for f in sorted(good, key=lambda f: -f.sharpness):
        if len(picked) >= count:
            break
        if f not in picked and new_view(f):
            picked.append(f)
    return sorted(f.index for f in picked)


def _resize(img: np.ndarray) -> np.ndarray:
    import cv2  # noqa: PLC0415
    h, w = img.shape[:2]
    scale = MAX_SIDE / max(h, w)
    return cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else img


def read_frames(video: Path) -> tuple[list[Frame], float]:
    """Прочитать видео с шагом ~SAMPLE_FPS кадров в секунду -> (кадры для выбора, кадров в секунду в видео)."""
    import cv2  # noqa: PLC0415
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise ValueError(f"не открывается видео {video.name} (формат не поддерживается?)")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(fps / SAMPLE_FPS))
    frames: list[Frame] = []
    i = 0
    try:
        while True:
            ok = cap.grab()
            if not ok:
                break
            if i % step == 0:
                ok, img = cap.retrieve()
                if ok and img is not None:
                    gray = cv2.cvtColor(_resize(img), cv2.COLOR_BGR2GRAY)
                    frames.append(Frame(i, sharpness(gray), cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)))
            i += 1
    finally:
        cap.release()
    return frames, fps


def save_frames(video: Path, indexes: list[int], folder: Path) -> int:
    import cv2  # noqa: PLC0415
    folder.mkdir(parents=True, exist_ok=True)
    wanted = set(indexes)
    cap = cv2.VideoCapture(str(video))
    saved, i = 0, 0
    try:
        while wanted:
            ok = cap.grab()
            if not ok:
                break
            if i in wanted:
                ok, img = cap.retrieve()
                if ok and img is not None:
                    saved += bool(cv2.imwrite(str(folder / f"v{i:05d}.jpg"), _resize(img), [cv2.IMWRITE_JPEG_QUALITY, 90]))
                wanted.discard(i)
            i += 1
    finally:
        cap.release()
    return saved


def append_lists(out: Path, sku: str, name: str, brand: str, category: str, barcode: str) -> None:
    """Строка в products.csv и barcodes.csv папки (форматы scripts/import_catalog.py и fetch_open_catalog --link-barcodes)."""
    products, barcodes = out / "products.csv", out / "barcodes.csv"
    rows = []
    if products.is_file():
        with products.open(encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f, delimiter=";") if r.get("sku") != sku]
    rows.append({"sku": sku, "name": name or sku, "brand": brand, "category": category, "price": ""})
    with products.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "name", "brand", "category", "price"], delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    if barcode:
        pairs = []
        if barcodes.is_file():
            with barcodes.open(encoding="utf-8", newline="") as f:
                pairs = [(r["sku"], r["barcode"]) for r in csv.DictReader(f, delimiter=";") if r.get("sku") != sku]
        pairs.append((sku, barcode))
        with barcodes.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["sku", "barcode"])
            w.writerows(pairs)


def one_video(video: Path, sku: str, out: Path, count: int) -> int:
    frames, _fps = read_frames(video)
    indexes = pick_frames(frames, count)
    saved = save_frames(video, indexes, out / sku)
    note = "" if saved >= 10 else " — мало: снимите медленнее и со всех сторон"
    print(f"{video.name} -> {sku}: {saved} фото{note}")
    return saved


def main() -> None:
    from app.services.catalog_import import validate_sku  # noqa: PLC0415
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", help="одно видео товара")
    ap.add_argument("--videos", help="папка с видео: имя файла = артикул")
    ap.add_argument("--sku", default="", help="--video: артикул (по умолчанию — имя файла)")
    ap.add_argument("--name", default="", help="--video: название, например «Pepsi 0,5 л» (объём — обязательно в названии)")
    ap.add_argument("--brand", default="")
    ap.add_argument("--category", default="", help="например Напитки; алкоголь — «Алкоголь (21+)», табак — «Табак (21+)»")
    ap.add_argument("--barcode", default="", help="штрихкод товара (у каждого размера — свой)")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT, help="сколько фото выбрать (по умолчанию 15)")
    ap.add_argument("--out", required=True, help="папка каталога (на диске D)")
    a = ap.parse_args()
    out = Path(a.out)
    jobs: list[tuple[Path, str]] = []
    if a.video:
        jobs.append((Path(a.video), a.sku or Path(a.video).stem))
    if a.videos:
        jobs += [(p, p.stem) for p in sorted(Path(a.videos).iterdir()) if p.suffix.lower() in VIDEO_EXTS]
    if not jobs:
        ap.error("укажите --video или --videos")
    for video, sku in jobs:
        problem = validate_sku(sku)
        if problem:
            print(f"{video.name}: артикул «{sku}» не подходит ({problem}) — пропускаю")
            continue
        try:
            one_video(video, sku, out, a.count)
        except ValueError as exc:
            print(exc)
            continue
        if a.video and video == Path(a.video):
            append_lists(out, sku, a.name, a.brand, a.category, a.barcode)
        elif not (out / "products.csv").is_file() or sku not in (out / "products.csv").read_text(encoding="utf-8-sig"):
            append_lists(out, sku, "", "", "", "")


if __name__ == "__main__":
    main()
