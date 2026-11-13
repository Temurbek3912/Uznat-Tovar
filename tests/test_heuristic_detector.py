"""
app/pipeline/detector.py::HeuristicContourDetector и suppress_nested_boxes — на синтетических полках (цветные коробки на
светлом фоне, как простой макет). Это НЕ доказательство качества на реальных фото: тесты фиксируют (1) что эвристика больше
не склеивает ярус полки в одну рамку (прежняя версия давала F1 = 0% даже здесь), (2) инварианты: рамки внутри кадра, нет рамки
размером с кадр, ничего не падает на пустых/мелких/шумных/серых изображениях, время ограничено.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

from app.pipeline.detector import HeuristicContourDetector, suppress_nested_boxes
from ml.detector_kit import match_boxes


def make_shelf(rows=3, cols=6, gap=14, seed=0, w=1280, h=720, textured=False, bg=235, noise=0):
    """Синтетическая полка: доски, на них цветные коробки. -> (изображение RGB, список истинных рамок xyxy)."""
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), bg, np.uint8)
    boxes = []
    top = 40
    row_h = (h - top - 40) // rows
    for r in range(rows):
        y_base = top + (r + 1) * row_h - 18
        cv2.rectangle(img, (20, y_base), (w - 20, y_base + 10), (90, 70, 50), -1)
        x = 40
        pw = (w - 80 - gap * (cols - 1)) // cols
        for _ in range(cols):
            ph = int(row_h * rng.uniform(0.55, 0.85))
            x1, x2, y2 = x, x + pw, y_base
            y1 = y2 - ph
            cv2.rectangle(img, (x1, y1), (x2, y2), tuple(int(v) for v in rng.integers(30, 230, 3)), -1)
            if textured:
                cv2.rectangle(img, (x1 + 8, y1 + ph // 3), (x2 - 8, y1 + 2 * ph // 3), tuple(int(v) for v in rng.integers(0, 255, 3)), -1)
                for _ in range(6):
                    cv2.circle(img, (int(rng.integers(x1 + 5, x2 - 5)), int(rng.integers(y1 + 5, y2 - 5))),
                               int(rng.integers(3, 9)), tuple(int(v) for v in rng.integers(0, 255, 3)), -1)
            boxes.append((x1, y1, x2, y2))
            x += pw + gap
    if noise:
        img = np.clip(img.astype(int) + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)
    return img, boxes


def _f1(detector, seeds=range(6), **scene):
    tp = fp = fn = 0
    for seed in seeds:
        img, truth = make_shelf(seed=seed, **scene)
        pred = [((d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2), d.confidence) for d in detector.detect(img)]
        pairs, f_p, f_n = match_boxes(pred, truth, 0.5)
        tp, fp, fn = tp + len(pairs), fp + len(f_p), fn + len(f_n)
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return 2 * p * r / (p + r) if p + r else 0.0, p, r


# ----------------------------------------------------------------------- качество на простых синтетических полках

def test_simple_shelf_is_no_longer_merged_into_one_box_per_row():
    f1, precision, recall = _f1(HeuristicContourDetector(), gap=14)
    assert f1 >= 0.65 and precision >= 0.9                 # замер при разработке: F1 ≈ 75%, точность 100% (прежняя версия: F1 = 0%)


def test_dense_shelf_and_noisy_textured_shelf_stay_usable_for_draft_labelling():
    assert _f1(HeuristicContourDetector(), gap=3)[0] >= 0.65                                 # замер: ≈ 79%
    assert _f1(HeuristicContourDetector(), gap=14, textured=True, noise=12)[0] >= 0.55        # замер: ≈ 70%


def test_old_failure_mode_is_gone_one_blob_per_row_would_give_zero_true_positives():
    img, truth = make_shelf(seed=1, gap=14)
    pred = [((d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2), d.confidence) for d in HeuristicContourDetector().detect(img)]
    pairs, _, _ = match_boxes(pred, truth, 0.5)
    assert len(pairs) >= 8                                  # из 18 товаров (прежняя версия: 0)


# ----------------------------------------------------------------------- инварианты

def test_boxes_are_inside_the_frame_with_positive_size_and_never_frame_sized():
    for kw in (dict(gap=14), dict(gap=0), dict(gap=3, textured=True, noise=20)):
        img, _ = make_shelf(seed=2, **kw)
        h, w = img.shape[:2]
        for d in HeuristicContourDetector().detect(img):
            b = d.bbox
            assert 0 <= b.x1 < b.x2 <= w and 0 <= b.y1 < b.y2 <= h
            assert (b.x2 - b.x1) * (b.y2 - b.y1) <= 0.25 * w * h      # max_area_ratio: фон/стеллаж — не товар
            assert d.confidence == 0.5


def test_degenerate_images_do_not_crash_and_give_nothing():
    det = HeuristicContourDetector()
    assert det.detect(np.full((300, 300, 3), 200, np.uint8)) == []                 # пустое однотонное
    assert det.detect(np.zeros((10, 10, 3), np.uint8)) == []                       # крошечное
    gray = make_shelf(seed=3)[0][:, :, 0]
    assert isinstance(det.detect(gray), list)                                      # одноканальное изображение


def test_result_is_deterministic():
    img, _ = make_shelf(seed=4, textured=True)
    a = [(d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2) for d in HeuristicContourDetector().detect(img)]
    b = [(d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2) for d in HeuristicContourDetector().detect(img)]
    assert a == b


def test_white_noise_photo_is_bounded_in_time_and_boxes():
    noise = np.random.default_rng(0).integers(0, 256, (720, 1280, 3), dtype=np.uint8)
    start = time.monotonic()
    out = HeuristicContourDetector().detect(noise)
    assert time.monotonic() - start < 5.0                                           # потолок кандидатов: нет O(n^2) взрыва
    assert len(out) <= HeuristicContourDetector.MAX_CANDIDATES


def test_constructor_parameters_change_behaviour():
    img, _ = make_shelf(seed=0, gap=14)
    default = HeuristicContourDetector().detect(img)
    assert HeuristicContourDetector(min_area_px=10 ** 7).detect(img) == []          # всё слишком мелкое
    tiny_limit = HeuristicContourDetector(max_area_ratio=0.001).detect(img)
    assert tiny_limit == [] and default                                             # всё слишком крупное для такого предела


def test_candidate_cap_limits_a_very_dense_scene_to_exactly_the_cap():
    img, truth = make_shelf(rows=12, cols=40, gap=3, w=2000, h=1400, seed=0)             # 480 мелких товаров
    out = HeuristicContourDetector().detect(img)
    assert len(truth) == 480 and len(out) == HeuristicContourDetector.MAX_CANDIDATES == 300


def test_non_rectangular_regions_are_rejected_unless_the_extent_filter_is_off():
    canvas = np.full((400, 400, 3), 255, np.uint8)                                       # Г-образная область: занимает ~44% своей рамки
    cv2.polylines(canvas, [np.array([(50, 50), (250, 50), (250, 100), (100, 100), (100, 250), (50, 250)], np.int32)], True, (0, 0, 0), 3)
    assert HeuristicContourDetector().detect(canvas) == []
    assert len(HeuristicContourDetector(min_extent=0.0).detect(canvas)) == 1


def test_very_thin_horizontal_and_vertical_bars_are_rejected_by_the_aspect_filter():
    bar = np.full((200, 500, 3), 255, np.uint8)
    cv2.rectangle(bar, (50, 90), (350, 106), (0, 0, 0), -1)
    tall = np.full((500, 200, 3), 255, np.uint8)
    cv2.rectangle(tall, (90, 50), (106, 350), (0, 0, 0), -1)
    det = HeuristicContourDetector()
    assert det.detect(bar) == [] and det.detect(tall) == []
    assert len(HeuristicContourDetector(min_aspect=0.0).detect(bar)) == 1
    assert len(HeuristicContourDetector(max_aspect=1000.0).detect(tall)) == 1


def test_padding_that_pushes_boxes_outside_the_frame_is_clamped():
    img, _ = make_shelf(seed=0, gap=14)
    h, w = img.shape[:2]
    boxes = [d.bbox for d in HeuristicContourDetector(pad=50).detect(img)]
    assert boxes and all(b.x1 >= 0 and b.y1 >= 0 and b.x2 <= w and b.y2 <= h for b in boxes)
    assert min(b.x1 for b in boxes) == 0.0                                                # левые товары упёрлись в край


# ----------------------------------------------------------------------- подавление вложенных рамок

def test_label_inside_a_package_is_dropped_but_the_package_stays():
    package, label = (0, 0, 100, 100), (20, 20, 60, 60)
    assert suppress_nested_boxes([package, label]) == [package]


def test_cavity_containing_several_boxes_is_dropped_and_its_contents_stay():
    cavity = (0, 0, 400, 100)
    items = [(10, 10, 90, 90), (110, 10, 190, 90), (210, 10, 290, 90)]
    kept = suppress_nested_boxes([cavity] + items)
    assert cavity not in kept and sorted(kept) == sorted(items)


def test_duplicates_keep_exactly_one_and_disjoint_or_partial_overlaps_are_untouched():
    box = (0, 0, 50, 50)
    assert suppress_nested_boxes([box, box]) == [box]
    a, b = (0, 0, 50, 50), (100, 0, 150, 50)
    assert sorted(suppress_nested_boxes([a, b])) == [a, b]
    c, d = (0, 0, 60, 60), (40, 0, 100, 60)                                         # перекрываются на треть — это разные товары
    assert sorted(suppress_nested_boxes([c, d])) == [c, d]
    assert suppress_nested_boxes([]) == []
