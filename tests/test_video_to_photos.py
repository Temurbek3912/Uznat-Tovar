"""Видео товара -> 10–15 разных чётких фото (scripts/video_to_photos.py)."""
import csv

import numpy as np
import pytest

from scripts import video_to_photos as v2p


def _frame(i: int, sharp: float, level: int) -> v2p.Frame:
    return v2p.Frame(i, sharp, np.full((32, 32), level, dtype=np.uint8))


def test_pick_frames_skips_blurry_and_repeats_spreads_over_video():
    # 60 кадров: вид меняется каждые 4 кадра (15 разных видов), каждый 4-й кадр смазан
    frames = [_frame(i, 5.0 if i % 4 == 3 else 100.0 + i % 4, (i // 4) * 15) for i in range(60)]
    picked = v2p.pick_frames(frames, 15)
    assert len(picked) == 15
    assert all(i % 4 != 3 for i in picked)                      # смазанных нет
    assert len({i // 4 for i in picked}) == 15                  # все 15 видов разные


def test_pick_frames_same_view_gives_one_photo():
    frames = [_frame(i, 100.0, 120) for i in range(40)]          # телефон не двигался — один вид
    assert len(v2p.pick_frames(frames, 15)) == 1
    assert v2p.pick_frames([], 15) == []


def test_video_to_folder_and_lists(tmp_path):
    cv2 = pytest.importorskip("cv2")
    video = tmp_path / "PEPSI-0.5L.avi"
    w = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 20, (160, 120))
    if not w.isOpened():
        pytest.skip("нет кодека для записи пробного видео")
    rng = np.random.default_rng(1)
    for i in range(80):                                          # 4 секунды: «товар» сдвигается — новый вид
        img = np.zeros((120, 160, 3), np.uint8)
        x = 10 + i
        img[20:100, x % 120:x % 120 + 30] = (40, 80, 200)
        img[30:40, :] = rng.integers(0, 255, (10, 160, 3), dtype=np.uint8)
        w.write(img)
    w.release()
    out = tmp_path / "catalog"
    saved = v2p.one_video(video, "PEPSI-0.5L", out, 12)
    assert 2 <= saved <= 12
    assert len(list((out / "PEPSI-0.5L").glob("*.jpg"))) == saved
    v2p.append_lists(out, "PEPSI-0.5L", "Pepsi 0,5 л", "Pepsi", "Напитки", "4780022620153")
    v2p.append_lists(out, "PEPSI-0.5L", "Pepsi 0,5 л", "Pepsi", "Напитки", "4780022620153")   # повтор не дублирует строку
    with (out / "products.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert [r["name"] for r in rows] == ["Pepsi 0,5 л"]
    assert (out / "barcodes.csv").read_text(encoding="utf-8").splitlines() == ["sku;barcode", "PEPSI-0.5L;4780022620153"]
