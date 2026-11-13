"""Скрипт ml/probe_detector.py (быстрый взгляд на детектор): логика вердиктов и отчёта проверяется на подставных детекторах —
без моделей, без pydantic и без интернета. Сама модель YOLO здесь не запускается; её проверяет пользователь на своих фото."""
from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace as NS

import numpy as np
from PIL import Image

from ml import probe_detector as pd


def det(x1, y1, x2, y2, conf):
    return NS(bbox=NS(x1=x1, y1=y1, x2=x2, y2=y2), confidence=conf)


class FakeDetector:
    """Для обычной картинки отдаёт заданные рамки; для картинки, перевёрнутой в BGR (первый пиксель-маркер), — другие."""

    def __init__(self, rgb, bgr):
        self.rgb, self.bgr = rgb, bgr
        self.calls = 0

    def detect(self, image):
        self.calls += 1
        return self.bgr if image[0, 0, 0] == 0 and image[0, 0, 2] == 255 else self.rgb


def make_photo(path, size=(200, 100)):
    arr = np.full((size[1], size[0], 3), 128, dtype=np.uint8)
    arr[0, 0] = (255, 0, 0)                  # маркер: красный пиксель в RGB; в BGR-копии он станет (0, 0, 255)
    Image.fromarray(arr).save(path)
    return path


def load_plain(path):
    return np.array(Image.open(path).convert("RGB"))


# ------------------------------------------------------------------ вердикты

def test_verdict_names_every_case():
    assert pd.verdict_of(1, 0) == pd.ONE and pd.verdict_of(1, 5) == pd.ONE
    assert pd.verdict_of(3, 0) == pd.SEVERAL
    assert pd.verdict_of(0, 2) == pd.WEAK
    assert pd.verdict_of(0, 0) == pd.NONE


def test_summarize_splits_strong_and_weak_by_the_working_threshold():
    boxes = [(10, 10, 110, 60, 0.80), (0, 0, 20, 20, 0.20), (0, 0, 5, 5, 0.05)]       # 0.05 ниже диагностического порога
    info = pd.summarize(boxes, (100, 200, 3), conf=0.35, low_conf=0.10)
    assert info["strong"] == 1 and info["weak"] == 1
    assert info["top"] == 0.80
    assert abs(info["share"] - (100 * 50) / (200 * 100)) < 1e-9          # доля кадра = площадь рамки / площадь кадра
    assert info["verdict"] == pd.ONE


def test_summarize_without_boxes_is_none_with_zero_share():
    info = pd.summarize([], (100, 200, 3), 0.35, 0.10)
    assert info == {"strong": 0, "weak": 0, "top": 0.0, "share": 0.0, "verdict": pd.NONE}


def test_only_weak_boxes_are_reported_as_weak_and_measured_from_the_weak_pool():
    info = pd.summarize([(0, 0, 100, 50, 0.2)], (100, 200, 3), 0.35, 0.10)
    assert info["verdict"] == pd.WEAK and info["strong"] == 0 and info["share"] > 0


def test_color_comparison_uses_a_margin():
    assert pd.compare_rgb_bgr(0.50, 0.52) == "одинаково"
    assert pd.compare_rgb_bgr(0.50, 0.70) == "BGR лучше"
    assert pd.compare_rgb_bgr(0.70, 0.50) == "RGB лучше"


# ------------------------------------------------------------------ прогон по папке

def test_probe_folder_reports_each_photo_draws_boxes_and_keeps_originals(tmp_path):
    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    good = make_photo(photos_dir / "one.png")
    before = good.read_bytes()
    detector = FakeDetector(rgb=[det(10, 10, 110, 60, 0.9)], bgr=[det(10, 10, 110, 60, 0.4)])
    rows = pd.probe_folder([good], detector, tmp_path / "out", conf=0.35, low_conf=0.10, load=load_plain)
    assert detector.calls == 2                                           # один раз как есть, один раз в BGR
    row = rows[0]
    assert row["verdict"] == pd.ONE and row["colors"] == "RGB лучше" and row["error"] == ""
    assert (tmp_path / "out" / "one__boxes.jpg").is_file()
    assert good.read_bytes() == before                                   # исходное фото не тронуто


def test_one_unreadable_photo_does_not_stop_the_rest(tmp_path):
    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    bad = photos_dir / "a_broken.jpg"
    bad.write_text("это не картинка", encoding="utf-8")
    good = make_photo(photos_dir / "b_good.png")
    detector = FakeDetector(rgb=[], bgr=[])
    rows = pd.probe_folder([bad, good], detector, tmp_path / "out", 0.35, 0.10, load=load_plain)
    assert rows[0]["verdict"] == "ошибка" and rows[0]["error"]
    assert rows[1]["verdict"] == pd.NONE and rows[1]["error"] == ""


def test_report_files_and_text_are_written_in_russian(tmp_path):
    rows = [{"file": "x.jpg", "strong": 1, "weak": 0, "top": 0.9, "share": 0.25, "verdict": pd.ONE, "bgr_strong": 1, "bgr_top": 0.9,
             "colors": "одинаково", "error": ""}]
    pd.write_csv(rows, tmp_path / "r" / "report.csv")
    raw = (tmp_path / "r" / "report.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                               # BOM: Excel покажет кириллицу правильно
    text = raw.decode("utf-8-sig")
    assert "Вердикт" in text and "один товар" in text and "25%" in text
    assert "Итого фото: 1" in pd.format_summary(rows, 0.35)
    assert "x.jpg" in pd.format_table(rows)


# ------------------------------------------------------------------ запуск из командной строки

def run_main(args):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = pd.main(args)
    return code, buffer.getvalue()


def test_main_explains_what_to_do_when_weights_are_missing(tmp_path):
    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    make_photo(photos_dir / "one.png")
    code, text = run_main(["--photos", str(photos_dir), "--out", str(tmp_path / "out"), "--weights", str(tmp_path / "нет.pt")])
    assert code == 2 and "Файл весов детектора не найден" in text and "sku110k_yolo11n.pt" in text
    assert not (tmp_path / "out").exists()                               # при ошибке ничего не создаётся


def test_main_rejects_missing_or_empty_photo_folder(tmp_path):
    code, text = run_main(["--photos", str(tmp_path / "нет"), "--out", str(tmp_path / "out")])
    assert code == 2 and "не найдена" in text
    (tmp_path / "empty").mkdir()
    code, text = run_main(["--photos", str(tmp_path / "empty"), "--out", str(tmp_path / "out")])
    assert code == 2 and "нет фото" in text


def test_main_rejects_inverted_thresholds(tmp_path):
    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    make_photo(photos_dir / "one.png")
    code, _ = run_main(["--photos", str(photos_dir), "--conf", "0.2", "--low-conf", "0.5"])
    assert code == 2
