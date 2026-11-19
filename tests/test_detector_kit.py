"""
ml/detector_kit.py — набор для детектора на своих фото. Выполняется реально (PIL/OpenCV/numpy): датасеты создаются во
временных папках, детекторы — подставные. Не проверяется: настоящее обучение YOLO (нужны ultralytics и GPU) —
оболочка train() проверена на подставной фабрике модели.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from PIL import Image

from app.core.schemas import BoundingBox
from app.pipeline.detector import RawDetection
from ml import detector_kit as dk
from ml.detector_kit import (
    DatasetError, YoloBox, boxes_to_yolo_lines, check_dataset, diagnose, evaluate_folder, iou, load_detector,
    match_boxes, parse_label_text, preannotate, split_dataset, train, yolo_to_xyxy,
)


# ----------------------------------------------------------------------- помощники

def _img(path: Path, w=200, h=100, color=(120, 130, 140)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), color).save(path)


def _lbl(path: Path, *lines: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


GOOD = "0 0.5 0.5 0.2 0.4"


def _dataset(root: Path, n_train=3, n_val=2, line=GOOD, extra="0 0.2 0.2 0.1 0.1"):
    for split, n in (("train", n_train), ("val", n_val)):
        for i in range(n):
            _img(root / "images" / split / f"{split}{i}.jpg")
            _lbl(root / "labels" / split / f"{split}{i}.txt", *([line, extra] if extra else [line]))
    return root


class FakeDetector:
    """Возвращает заранее заданные рамки (x1,y1,x2,y2,conf) для любого изображения."""
    def __init__(self, boxes):
        self.boxes = boxes

    def detect(self, image):
        return [RawDetection(bbox=BoundingBox(x1=b[0], y1=b[1], x2=b[2], y2=b[3]), confidence=b[4]) for b in self.boxes]


# ----------------------------------------------------------------------- формат и геометрия

def test_parse_label_valid_empty_and_blank_lines():
    boxes, errs = parse_label_text("0 0.5 0.5 0.2 0.4\n\n  \n0 0.1 0.1 0.1 0.1\n")
    assert errs == [] and boxes[0] == YoloBox(0.5, 0.5, 0.2, 0.4, 0) and len(boxes) == 2
    assert parse_label_text("") == ([], []) and parse_label_text("\n\n") == ([], [])


def test_parse_label_reports_each_kind_of_mistake_with_line_numbers():
    text = "\n".join([
        "0 0.5 0.5 0.2",                       # 2: мало полей
        "0 0.1 0.2 0.3 0.4 0.5 0.6 0.7",       # 3: полигон
        "a b c d e",                            # 4: не числа
        "1 0.5 0.5 0.1 0.1",                    # 5: класс вне диапазона
        "0 0.5 0.5 0 0.1",                      # 6: нулевая ширина
        "0 320 240 50 80",                      # 7: пиксели вместо нормированных
        "0 0.95 0.5 0.2 0.2",                   # 8: выходит за кадр
        "0 0.5 0.5 0.2 0.2",                    # 9: нормальная
    ])
    boxes, errs = parse_label_text(text)
    assert len(boxes) == 1
    joined = "\n".join(errs)
    assert "строка 1: ожидалось 5 чисел" in joined and "строка 2" in joined and "полигонами" in joined
    assert "строка 3: не числа" in joined and "строка 4: класс 1 вне диапазона" in joined
    assert "строка 5: нулевая" in joined and "строка 6" in joined and "пиксел" in joined and "строка 7" in joined and "границы" in joined
    assert len(errs) == 7


def test_boxes_to_yolo_lines_roundtrip_clamp_swap_and_degenerate():
    lines = boxes_to_yolo_lines([(10, 20, 110, 60)], 200, 100)
    box = parse_label_text(lines[0])[0][0]
    x1, y1, x2, y2 = yolo_to_xyxy(box, 200, 100)
    assert (round(x1), round(y1), round(x2), round(y2)) == (10, 20, 110, 60)
    clamped = parse_label_text(boxes_to_yolo_lines([(-30, -5, 50, 40)], 200, 100)[0])[0][0]
    assert yolo_to_xyxy(clamped, 200, 100)[:2] == (0.0, 0.0)                         # вылет за кадр обрезан
    swapped = boxes_to_yolo_lines([(110, 60, 10, 20)], 200, 100)
    assert swapped == lines                                                          # перепутанные углы исправлены
    assert boxes_to_yolo_lines([(10, 10, 11, 50), (5, 5, 5, 5), (300, 10, 320, 40)], 200, 100) == []   # вырожденные/вне кадра


def test_iou_cases():
    a = (0, 0, 10, 10)
    assert iou(a, a) == 1.0 and iou(a, (20, 20, 30, 30)) == 0.0
    assert iou(a, (5, 0, 15, 10)) == pytest.approx(1 / 3)
    assert iou(a, (2, 2, 4, 4)) == pytest.approx(4 / 100)                           # вложенная
    assert iou((0, 0, 0, 0), (0, 0, 0, 0)) == 0.0


def test_match_boxes_is_one_to_one_by_confidence_and_respects_the_threshold():
    truth = [(0, 0, 10, 10), (20, 0, 30, 10)]
    pred = [((0, 0, 10, 10), 0.9), ((0, 0, 10, 10), 0.8), ((21, 0, 31, 10), 0.7), ((100, 100, 110, 110), 0.6)]
    pairs, fp, fn = match_boxes(pred, truth)
    assert sorted(pairs) == [(0, 0), (2, 1)] and sorted(fp) == [1, 3] and fn == []        # дубль — лишний, не второй TP
    pairs, fp, fn = match_boxes([((0, 0, 10, 10), 0.5)], [(5, 0, 15, 10)], iou_thr=0.5)
    assert pairs == [] and fp == [0] and fn == [0]                                       # IoU = 1/3 < 0.5
    assert match_boxes([], truth) == ([], [], [0, 1]) and match_boxes(pred, []) == ([], [0, 1, 2, 3], [])


def test_lower_confidence_prediction_cannot_steal_a_better_match():
    truth = [(0, 0, 10, 10)]
    pairs, fp, fn = match_boxes([((1, 1, 10, 10), 0.4), ((0, 0, 10, 10), 0.9)], truth)
    assert pairs == [(1, 0)] and fp == [0]                                               # первым разбирается самая уверенная


# ----------------------------------------------------------------------- проверка датасета

def test_valid_dataset_passes_and_counts_are_right(tmp_path):
    rep = check_dataset(_dataset(tmp_path / "ds"))
    assert rep.ok and rep.images == {"train": 3, "val": 2} and rep.boxes == {"train": 6, "val": 4}
    assert rep.unlabeled == {"train": 0, "val": 0}
    assert "Датасет годится" in dk.format_dataset_report(rep)


def test_missing_split_folder_empty_folder_and_corrupt_image_are_errors(tmp_path):
    root = _dataset(tmp_path / "ds")
    (root / "images" / "val" / "val0.jpg").write_bytes(b"not an image")
    rep = check_dataset(root)
    assert not rep.ok and any("повреждённые" in e and "val0.jpg" in e for e in rep.errors)
    import shutil
    shutil.rmtree(root / "images" / "val")
    rep = check_dataset(root)
    assert any("нет папки" in e for e in rep.errors)
    (root / "images" / "val").mkdir()
    assert any("нет изображений" in e for e in check_dataset(root).errors)


def test_label_mistakes_are_errors_with_file_names_and_are_capped(tmp_path):
    root = _dataset(tmp_path / "ds", n_train=12, n_val=1)
    for i in range(12):
        _lbl(root / "labels" / "train" / f"train{i}.txt", "0 320 240 50 80")                # пиксели вместо нормированных
    rep = check_dataset(root, max_listed=3)
    assert not rep.ok and sum("train/train" in e for e in rep.errors) == 3
    assert any("ещё 9 ошибок" in e for e in rep.errors)


def test_orphan_labels_overlapping_names_and_unlabeled_images_are_warnings(tmp_path):
    root = _dataset(tmp_path / "ds", n_train=4, n_val=2)
    _lbl(root / "labels" / "train" / "ghost.txt", GOOD)
    _img(root / "images" / "val" / "train0.jpg")                                         # то же имя, что в train
    (root / "labels" / "train" / "train1.txt").unlink()
    (root / "labels" / "train" / "train2.txt").write_text("")
    (root / "labels" / "train" / "train3.txt").write_text("")
    rep = check_dataset(root)
    assert rep.ok
    text = "\n".join(rep.warnings)
    assert "разметка без фото в train: ghost" in text and "одинаковые имена" in text and "train0" in text
    assert rep.unlabeled["train"] == 3 and "3 из 4 фото без рамок" in text


def test_small_dataset_few_boxes_and_small_objects_warnings(tmp_path):
    root = _dataset(tmp_path / "ds", line="0 0.5 0.5 0.01 0.01", extra=None)
    rep = check_dataset(root)
    text = "\n".join(rep.warnings)
    assert rep.ok and "мало обучающих фото: 3" in text and "мало размеченных рамок" in text and "объекты мелкие" in text
    assert rep.median_box_height == pytest.approx(0.01)
    # крупные и мелкие рамки поровну: медиана между ними, предупреждения о мелких объектах нет
    mixed = check_dataset(_dataset(tmp_path / "ds2", line="0 0.5 0.5 0.01 0.01"))
    assert mixed.median_box_height == pytest.approx(0.055) and not any("объекты мелкие" in w for w in mixed.warnings)


def test_non_image_files_in_image_folder_are_ignored(tmp_path):
    root = _dataset(tmp_path / "ds")
    (root / "images" / "train" / "notes.txt").write_text("x")
    (root / "images" / "train" / ".DS_Store").write_text("x")
    assert check_dataset(root).images["train"] == 3


# ----------------------------------------------------------------------- разбиение

def _flat(src: Path, n=10, labeled=True):
    for i in range(n):
        _img(src / f"p{i:02d}.jpg")
        if labeled:
            _lbl(src / f"p{i:02d}.txt", GOOD)
    return src


def test_split_creates_yolo_layout_yaml_and_a_dataset_that_passes_check(tmp_path):
    src = _flat(tmp_path / "photos", 10)
    r = split_dataset(src, tmp_path / "ds")
    assert (r.train, r.val, r.skipped_unlabeled) == (8, 2, 0)
    yaml_text = r.data_yaml.read_text(encoding="utf-8")
    assert "train: images/train" in yaml_text and "val: images/val" in yaml_text and "0: product" in yaml_text
    assert (tmp_path / "ds").resolve().as_posix() in yaml_text
    rep = check_dataset(tmp_path / "ds")
    assert rep.ok and rep.images == {"train": 8, "val": 2}
    assert len(list(src.glob("*.jpg"))) == 10 and len(list(src.glob("*.txt"))) == 10        # исходники не тронуты


def test_split_is_deterministic_per_seed_and_differs_between_seeds(tmp_path):
    src = _flat(tmp_path / "photos", 20)
    def val_names(dst, seed):
        split_dataset(src, tmp_path / dst, seed=seed)
        return sorted(p.name for p in (tmp_path / dst / "images" / "val").iterdir())
    assert val_names("a", 1) == val_names("b", 1) and val_names("a2", 1) != val_names("c", 2)


def test_split_ratio_edge_cases_always_leave_both_splits_non_empty(tmp_path):
    r = split_dataset(_flat(tmp_path / "p2", 2), tmp_path / "d2")
    assert (r.train, r.val) == (1, 1)
    r = split_dataset(_flat(tmp_path / "p5", 5), tmp_path / "d5", val_ratio=0.9)
    assert r.train >= 1 and r.val >= 1 and r.train + r.val == 5
    with pytest.raises(DatasetError, match="между 0 и 1"):
        split_dataset(_flat(tmp_path / "p6", 6), tmp_path / "d6", val_ratio=1.5)


def test_split_finds_labels_in_every_supported_place(tmp_path):
    src = tmp_path / "photos"
    for name in ("next_to", "in_labels_dir", "in_src_labels"):
        _img(src / f"{name}.jpg")
    _lbl(src / "next_to.txt", GOOD)
    _lbl(tmp_path / "ext" / "in_labels_dir.txt", GOOD)
    _lbl(src / "labels" / "in_src_labels.txt", GOOD)
    r = split_dataset(src, tmp_path / "ds", labels_dir=tmp_path / "ext")
    assert r.skipped_unlabeled == 0 and r.train + r.val == 3


def test_split_skips_unlabeled_by_default_and_can_include_them_as_background(tmp_path):
    src = _flat(tmp_path / "photos", 4)
    _img(src / "bare.jpg")
    r = split_dataset(src, tmp_path / "a")
    assert r.skipped_unlabeled == 1 and r.train + r.val == 4
    r = split_dataset(src, tmp_path / "b", include_unlabeled=True)
    assert r.skipped_unlabeled == 0 and r.train + r.val == 5
    bare = next((tmp_path / "b" / "labels").rglob("bare.txt"))
    assert bare.read_text() == ""                                                          # фон = пустая разметка


def test_split_refuses_non_empty_destination_duplicates_and_too_few_images(tmp_path):
    src = _flat(tmp_path / "photos", 4)
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "old.txt").write_text("x")
    with pytest.raises(DatasetError, match="не пуста"):
        split_dataset(src, tmp_path / "busy")
    _img(src / "sub" / "p00.png")                                                          # то же имя, другая папка/расширение
    with pytest.raises(DatasetError, match="повторяющиеся имена"):
        split_dataset(src, tmp_path / "ds")
    with pytest.raises(DatasetError, match="минимум 2"):
        split_dataset(_flat(tmp_path / "one", 1), tmp_path / "d1")


# ----------------------------------------------------------------------- предразметка

def test_preannotate_writes_yolo_files_classes_txt_and_does_not_overwrite_manual_labels(tmp_path):
    images = tmp_path / "photos"
    _img(images / "a.jpg", 200, 100)
    _img(images / "b.jpg", 200, 100)
    (images / "readme.txt").write_text("x")
    _lbl(images / "b.txt", "0 0.5 0.5 0.1 0.1")                                           # ручная разметка
    det = FakeDetector([(20, 10, 120, 60, 0.9), (150, 30, 400, 90, 0.8)])                  # вторая рамка выходит за кадр
    r = preannotate(images, images, det)
    assert (r.written, r.skipped_existing, r.boxes) == (1, 1, 2)
    assert (images / "b.txt").read_text() == "0 0.5 0.5 0.1 0.1\n"                         # не тронута
    boxes, errs = parse_label_text((images / "a.txt").read_text())
    assert errs == [] and len(boxes) == 2 and boxes[1].cx + boxes[1].w / 2 <= 1.0 + 1e-6   # обрезана по кадру
    assert (images / "classes.txt").read_text() == "product\n"
    r = preannotate(images, images, det, overwrite=True)
    assert r.written == 2 and (images / "b.txt").read_text() != "0 0.5 0.5 0.1 0.1\n"


def test_preannotate_with_no_detections_writes_empty_files_and_keeps_existing_classes_txt(tmp_path):
    images = tmp_path / "p"
    _img(images / "a.jpg")
    (images / "classes.txt").write_text("custom\n")
    r = preannotate(images, tmp_path / "out", FakeDetector([]))
    assert r.written == 1 and r.boxes == 0 and (tmp_path / "out" / "a.txt").read_text() == ""
    preannotate(images, images, FakeDetector([]))
    assert (images / "classes.txt").read_text() == "custom\n"


def test_preannotate_and_evaluate_see_phone_photos_upright(tmp_path):
    images = tmp_path / "p"
    images.mkdir()
    exif = Image.Exif()
    exif[0x0112] = 6                                                            # портрет, записанный как альбом 200x100
    Image.new("RGB", (200, 100), (90, 90, 90)).save(images / "phone.jpg", exif=exif)
    seen = []

    class Recording:
        def detect(self, image):
            seen.append(image.shape)
            return []
    preannotate(images, images, Recording())
    assert seen == [(200, 100, 3)]                                              # высота 200, ширина 100 — как в редакторе разметки


def test_load_detector_falls_back_to_heuristic_without_weights(tmp_path):
    det, desc = load_detector(str(tmp_path / "missing.pt"))
    assert type(det).__name__ == "HeuristicContourDetector" and "эвристика" in desc
    assert type(load_detector(None)[0]).__name__ == "HeuristicContourDetector"


# ----------------------------------------------------------------------- оценка

def _eval_set(tmp_path, n=3):
    images, labels = tmp_path / "img", tmp_path / "lbl"
    for i in range(n):
        _img(images / f"s{i}.jpg", 200, 100)
        _lbl(labels / f"s{i}.txt", "0 0.25 0.5 0.2 0.4", "0 0.75 0.5 0.2 0.4")             # 40..80 и 140..180 по X, 30..70 по Y
    return images, labels


TRUE_BOXES = [(40, 30, 80, 70, 0.9), (140, 30, 180, 70, 0.9)]


def test_perfect_detector_scores_f1_one_and_gets_the_good_verdict(tmp_path):
    images, labels = _eval_set(tmp_path)
    rep = evaluate_folder(images, labels, FakeDetector(TRUE_BOXES))
    assert (rep.tp, rep.fp, rep.fn) == (6, 0, 0) and rep.f1 == 1.0 and rep.mean_pred_to_true_area == pytest.approx(1.0)
    assert "хороший" in dk.format_eval_report(rep)


def test_empty_detector_has_zero_recall_and_extra_boxes_lower_precision(tmp_path):
    images, labels = _eval_set(tmp_path)
    rep = evaluate_folder(images, labels, FakeDetector([]))
    assert (rep.tp, rep.fn, rep.recall, rep.f1) == (0, 6, 0.0, 0.0) and "слабый" in dk.format_eval_report(rep)
    noisy = evaluate_folder(images, labels, FakeDetector(TRUE_BOXES + [(0, 0, 20, 20, 0.3), (90, 0, 110, 20, 0.3)]))
    assert (noisy.tp, noisy.fp, noisy.fn) == (6, 6, 0) and noisy.precision == 0.5 and noisy.recall == 1.0


def test_images_without_labels_are_skipped_and_no_pairs_is_an_error(tmp_path):
    images, labels = _eval_set(tmp_path, 2)
    _img(images / "unlabeled.jpg")
    assert len(evaluate_folder(images, labels, FakeDetector(TRUE_BOXES)).per_image) == 2
    with pytest.raises(DatasetError, match="нет ни одной пары"):
        evaluate_folder(images, tmp_path / "empty_labels", FakeDetector([]))


def test_malformed_label_stops_evaluation_with_file_name(tmp_path):
    images, labels = _eval_set(tmp_path, 1)
    _lbl(labels / "s0.txt", "0 300 200 40 40")
    with pytest.raises(DatasetError, match="s0.txt"):
        evaluate_folder(images, labels, FakeDetector([]))


def test_overlay_draws_green_red_and_blue_marks_on_the_array():
    rgb = np.full((100, 200, 3), 128, dtype=np.uint8)
    truth = [(40, 30, 80, 70), (140, 30, 180, 70)]
    pred = [(40, 30, 80, 70), (100, 5, 120, 25)]            # первая найдена верно, вторая лишняя, вторая размеченная пропущена
    pairs, fp, fn = match_boxes([(p, 0.9) for p in pred], truth)
    bgr = dk.draw_overlay(rgb, pred, truth, pairs, fp, fn)
    assert bgr.shape == (100, 200, 3) and rgb[30, 40].tolist() == [128, 128, 128]          # исходное изображение не испорчено
    assert bgr[30, 40].tolist() == [0, 200, 0]                  # верхний левый угол найденной рамки: зелёный (BGR)
    assert bgr[5, 100].tolist() == [0, 0, 230]                  # лишняя: красный (BGR)
    assert bgr[30, 140].tolist() == [230, 90, 0]                # пропущенная: синий (BGR)


def test_overlay_files_are_written_for_every_labeled_image(tmp_path):
    images, labels = _eval_set(tmp_path, 3)
    evaluate_folder(images, labels, FakeDetector(TRUE_BOXES), overlay_dir=tmp_path / "ov")
    files = sorted(p.name for p in (tmp_path / "ov").iterdir())
    with Image.open(tmp_path / "ov" / "s0.jpg") as overlay:
        assert files == ["s0.jpg", "s1.jpg", "s2.jpg"] and overlay.size == (200, 100)


def test_diagnose_gives_specific_advice():
    merged = dk.EvalReport([dk.ImageEval("a", 10, 3, 1, 2, 9)], 0.5, mean_pred_to_true_area=6.0)
    tips = " ".join(diagnose(merged))
    assert "слабый" in tips and "склеиваются" in tips and "пропускает" in tips
    spam = dk.EvalReport([dk.ImageEval("a", 5, 30, 5, 25, 0)], 0.5, mean_pred_to_true_area=1.0)
    assert "много лишних" in " ".join(diagnose(spam))
    ok = dk.EvalReport([dk.ImageEval("a", 10, 10, 9, 1, 1)], 0.5, mean_pred_to_true_area=1.0)
    assert "хороший" in " ".join(diagnose(ok)) and "слабый" not in " ".join(diagnose(ok))


def test_verdict_thresholds_at_the_boundaries():
    def verdict(tp, fp, fn):
        rep = dk.EvalReport([dk.ImageEval("a", tp + fn, tp + fp, tp, fp, fn)], 0.5, mean_pred_to_true_area=1.0)
        text = " ".join(diagnose(rep))
        return "хороший" if "хороший" in text else "средний" if "средний" in text else "слабый" if "слабый" in text else "?"
    assert verdict(17, 3, 3) == "хороший"        # F1 = 0.85 ровно
    assert verdict(21, 4, 4) == "средний"        # F1 = 0.84
    assert verdict(3, 2, 2) == "средний"         # F1 = 0.60 ровно
    assert verdict(13, 9, 9) == "слабый"         # F1 ≈ 0.59


# ----------------------------------------------------------------------- обучение (подставная модель)

class FakeYolo:
    created = []

    def __init__(self, base, save_dir: Path, via_trainer=False):
        self.base, self.train_kwargs, self.save_dir, self.via = base, None, save_dir, via_trainer
        (save_dir / "weights").mkdir(parents=True, exist_ok=True)
        (save_dir / "weights" / "best.pt").write_bytes(b"BEST-WEIGHTS")
        self.trainer = NS(save_dir=save_dir)

    def train(self, **kw):
        self.train_kwargs = kw
        return NS() if self.via else NS(save_dir=self.save_dir)


def _factory(tmp_path, **kw):
    holder = {}
    def make(base):
        holder["model"] = FakeYolo(base, tmp_path / "runs" / "shelf", **kw)
        return holder["model"]
    return make, holder


def test_train_runs_with_gpu_defaults_and_copies_best_weights_into_the_project(tmp_path):
    split = split_dataset(_flat(tmp_path / "photos", 10), tmp_path / "ds")
    make, holder = _factory(tmp_path)
    lines = []
    target = train(split.data_yaml, out_weights=str(tmp_path / "models" / "det.pt"), yolo_factory=make,
                   cuda_available=lambda: True, out=lines.append, project=str(tmp_path / "runs"))
    kw = holder["model"].train_kwargs
    assert holder["model"].base == "yolov8s.pt" and kw["data"] == str(split.data_yaml)
    assert kw["device"] == 0 and kw["batch"] == -1 and kw["imgsz"] == 1280 and kw["epochs"] == 80 and kw["single_cls"] is True
    assert target.read_bytes() == b"BEST-WEIGHTS" and "Готово" in "\n".join(lines)


def test_train_on_cpu_warns_and_uses_a_small_batch(tmp_path):
    split = split_dataset(_flat(tmp_path / "photos", 10), tmp_path / "ds")
    make, holder = _factory(tmp_path)
    lines = []
    train(split.data_yaml, out_weights=str(tmp_path / "w.pt"), yolo_factory=make, cuda_available=lambda: False, out=lines.append)
    kw = holder["model"].train_kwargs
    assert kw["device"] == "cpu" and kw["batch"] == 4 and "очень медленное" in "\n".join(lines)
    make2, holder2 = _factory(tmp_path)
    train(split.data_yaml, out_weights=str(tmp_path / "w2.pt"), yolo_factory=make2, cuda_available=lambda: True, device=1, batch=16, epochs=5, imgsz=640)
    kw = holder2["model"].train_kwargs
    assert (kw["device"], kw["batch"], kw["epochs"], kw["imgsz"]) == (1, 16, 5, 640)                # явные параметры побеждают


def test_train_keeps_a_backup_of_previous_weights(tmp_path):
    split = split_dataset(_flat(tmp_path / "photos", 10), tmp_path / "ds")
    out = tmp_path / "models" / "det.pt"
    out.parent.mkdir()
    out.write_bytes(b"OLD-WEIGHTS")
    make, _ = _factory(tmp_path)
    train(split.data_yaml, out_weights=str(out), yolo_factory=make, cuda_available=lambda: True, out=lambda m: None)
    backups = list(out.parent.glob("det.*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"OLD-WEIGHTS" and out.read_bytes() == b"BEST-WEIGHTS"


def test_train_finds_weights_through_trainer_when_results_have_no_save_dir(tmp_path):
    split = split_dataset(_flat(tmp_path / "photos", 10), tmp_path / "ds")
    make, _ = _factory(tmp_path, via_trainer=True)
    target = train(split.data_yaml, out_weights=str(tmp_path / "w.pt"), yolo_factory=make, cuda_available=lambda: True, out=lambda m: None)
    assert target.read_bytes() == b"BEST-WEIGHTS"


def test_train_refuses_a_broken_dataset_and_a_missing_yaml_without_touching_the_model(tmp_path):
    calls = []
    split = split_dataset(_flat(tmp_path / "photos", 10), tmp_path / "ds")
    next((tmp_path / "ds" / "labels" / "train").glob("*.txt")).write_text("0 500 500 10 10")
    with pytest.raises(DatasetError, match="не прошёл проверку"):
        train(split.data_yaml, yolo_factory=lambda b: calls.append(b), cuda_available=lambda: True, out=lambda m: None)
    with pytest.raises(DatasetError, match="нет файла"):
        train(tmp_path / "nope.yaml", yolo_factory=lambda b: calls.append(b))
    assert calls == []


def test_train_reports_when_no_best_weights_appear(tmp_path):
    split = split_dataset(_flat(tmp_path / "photos", 10), tmp_path / "ds")
    class NoWeights:
        def __init__(self, base): self.trainer = None
        def train(self, **kw): return NS()
    with pytest.raises(DatasetError, match="best.pt не найден"):
        train(split.data_yaml, out_weights=str(tmp_path / "w.pt"), yolo_factory=NoWeights, cuda_available=lambda: True,
              out=lambda m: None, project=str(tmp_path / "empty_runs"))


# ----------------------------------------------------------------------- командная строка

def test_cli_split_check_and_errors(tmp_path):
    lines = []
    src = _flat(tmp_path / "photos", 10)
    assert dk.main(["split", "--src", str(src), "--dst", str(tmp_path / "ds")], out=lines.append) == 0
    assert "train: 8, val: 2" in "\n".join(lines)
    assert dk.main(["check", "--dataset", str(tmp_path / "ds")], out=lines.append) == 0
    assert dk.main(["split", "--src", str(src), "--dst", str(tmp_path / "ds")], out=lines.append) == 1       # dst уже не пуста
    assert "Ошибка:" in lines[-1]
    next((tmp_path / "ds" / "labels" / "val").glob("*.txt")).write_text("0 1 1 1 1 1")
    assert dk.main(["check", "--dataset", str(tmp_path / "ds")], out=lines.append) == 1


def test_cli_evaluate_prints_report_and_saves_overlays_and_json(tmp_path):
    images, labels = _eval_set(tmp_path)
    lines = []
    code = dk.main(["evaluate", "--images", str(images), "--labels", str(labels), "--weights", str(tmp_path / "none.pt"),
                    "--out", str(tmp_path / "ev")], out=lines.append)
    assert code == 0 and "эвристика" in lines[0] and "F1" in "\n".join(lines)
    assert (tmp_path / "ev" / "s0.jpg").exists() and (tmp_path / "ev" / "report.json").exists()
    import json
    assert set(json.loads((tmp_path / "ev" / "report.json").read_text(encoding="utf-8"))) >= {"precision", "recall", "f1", "per_image"}


def test_cli_preannotate_end_to_end_with_heuristic(tmp_path):
    images = tmp_path / "p"
    _img(images / "a.jpg", 300, 200)
    lines = []
    assert dk.main(["preannotate", "--images", str(images), "--out", str(images), "--weights", str(tmp_path / "none.pt")], out=lines.append) == 0
    assert (images / "a.txt").exists() and (images / "classes.txt").exists() and "LabelImg" in "\n".join(lines)


# ----------------------------------------------------------------------- согласованность документации, конфигурации и кода

ROOT = Path(__file__).resolve().parent.parent


def test_every_command_in_the_quickstart_parses_with_the_real_cli():
    import re
    import shlex
    text = (ROOT / "docs" / "detector_quickstart.md").read_text(encoding="utf-8")
    commands = re.findall(r"^python -m ml\.detector_kit (.+)$", text, re.MULTILINE)
    assert len(commands) >= 5                                                     # preannotate, split, check, train, evaluate
    parser = dk.build_parser()
    seen = set()
    for cmd in commands:
        ns = parser.parse_args(shlex.split(cmd.split("#")[0]))                   # SystemExit при выдуманном флаге/подкоманде
        seen.add(ns.cmd)
    assert seen == {"preannotate", "split", "check", "train", "evaluate"}


def test_flags_mentioned_in_the_quickstart_text_exist():
    import re
    text = (ROOT / "docs" / "detector_quickstart.md").read_text(encoding="utf-8")
    known = {a for sp in dk.build_parser()._subparsers._group_actions[0].choices.values()
             for act in sp._actions for a in act.option_strings}
    for flag in set(re.findall(r"`(--[a-z-]+)", text)):
        assert flag in known, flag


def test_trained_weights_go_exactly_where_the_worker_and_config_look_for_them():
    cfg = (ROOT / "configs" / "config.yaml").read_text(encoding="utf-8")
    assert f'weights_path: "{dk.DEFAULT_WEIGHTS_OUT}"' in cfg                      # иначе обученный детектор тихо не используется
    runtime = (ROOT / "app" / "pipeline" / "ml_runtime.py").read_text(encoding="utf-8")
    # воркер строит детектор через ml_runtime: путь берётся именно из конфигурации и разрешается от корня проекта
    assert "_resolve(cfg.weights_path, resolve_path)" in runtime and "f.detector(weights, cfg)" in runtime
    assert "build_runtime(settings" in (ROOT / "app" / "worker.py").read_text(encoding="utf-8")
    assert dk.DEFAULT_WEIGHTS_OUT in (ROOT / "docs" / "detector_quickstart.md").read_text(encoding="utf-8")


def test_parser_defaults_match_what_the_guide_promises():
    ns = dk.build_parser().parse_args(["train", "--data", "d/data.yaml"])
    assert (ns.base, ns.epochs, ns.imgsz, ns.out_weights) == ("yolov8s.pt", 80, 1280, dk.DEFAULT_WEIGHTS_OUT)
    ns = dk.build_parser().parse_args(["split", "--src", "a", "--dst", "b"])
    assert ns.val == 0.2 and ns.seed == 0 and ns.include_unlabeled is False
