"""
app/pipeline/imageio.py — EXIF-поворот при чтении снимков (фото с телефона!) + сторож «прямого Image.open в проекте нет».
Реально выполняется (PIL). Проверяется на всех 8 значениях тега Orientation, а не на одном «типичном» случае.
"""
from __future__ import annotations

import ast
import io
from pathlib import Path

import numpy as np
from PIL import Image

from app.pipeline.imageio import load_rgb

ROOT = Path(__file__).resolve().parent.parent
T = Image.Transpose


def _upright() -> Image.Image:
    """Несимметричная картинка 6x4: любое неверное преобразование видно по пикселям."""
    arr = np.zeros((4, 6, 3), np.uint8)
    arr[0, 0] = (255, 0, 0)        # красный — левый верхний
    arr[0, 5] = (0, 255, 0)        # зелёный — правый верхний
    arr[3, 0] = (0, 0, 255)        # синий — левый нижний
    arr[2, 3] = (255, 255, 0)      # жёлтый — в глубине
    return Image.fromarray(arr)


# Что надо сделать с «правильной» картинкой, чтобы получить то, что лежит в файле при данном теге (обратное к exif_transpose).
INVERSE_OP = {2: T.FLIP_LEFT_RIGHT, 3: T.ROTATE_180, 4: T.FLIP_TOP_BOTTOM, 5: T.TRANSPOSE,
              6: T.ROTATE_90, 7: T.TRANSVERSE, 8: T.ROTATE_270}


def _save_png_with_orientation(path: Path, stored: Image.Image, orientation: int) -> None:
    exif = Image.Exif()
    exif[0x0112] = orientation
    stored.save(path, exif=exif)


def test_all_eight_exif_orientations_come_out_upright(tmp_path):
    upright = np.array(_upright())
    for k in range(1, 9):
        stored = _upright() if k == 1 else _upright().transpose(INVERSE_OP[k])
        p = tmp_path / f"o{k}.png"
        _save_png_with_orientation(p, stored, k)
        loaded = load_rgb(p)
        assert loaded.shape == upright.shape and (loaded == upright).all(), f"ориентация {k}"


def test_portrait_phone_photo_stored_as_landscape_becomes_portrait_jpeg(tmp_path):
    landscape = Image.new("RGB", (200, 100), (10, 120, 200))                      # пиксели «как с датчика»: 200x100
    exif = Image.Exif()
    exif[0x0112] = 6                                                              # «повернуть на 90° по часовой» — типично для портрета
    p = tmp_path / "phone.jpg"
    landscape.save(p, exif=exif)
    assert load_rgb(p).shape == (200, 100, 3)                                      # высота 200, ширина 100 — полка стоит вертикально
    with Image.open(p) as raw:                                                     # exif-ok: доказываем, что БЕЗ нормализации было бы боком
        assert raw.size == (200, 100)


def test_files_without_exif_are_untouched_and_all_input_kinds_work(tmp_path):
    arr = np.array(_upright())
    p = tmp_path / "plain.png"
    Image.fromarray(arr).save(p)
    assert (load_rgb(p) == arr).all() and (load_rgb(str(p)) == arr).all()
    assert (load_rgb(p.read_bytes()) == arr).all() and (load_rgb(bytearray(p.read_bytes())) == arr).all()
    with open(p, "rb") as fh:
        assert (load_rgb(fh) == arr).all()
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    buf.seek(0)
    assert (load_rgb(buf) == arr).all()


def test_other_colour_modes_become_rgb_uint8(tmp_path):
    for mode, name in (("RGBA", "a.png"), ("L", "l.png"), ("P", "p.png"), ("1", "b.png")):
        Image.new(mode, (8, 6)).save(tmp_path / name)
        out = load_rgb(tmp_path / name)
        assert out.shape == (6, 8, 3) and out.dtype == np.uint8, mode


def test_missing_or_broken_files_raise_instead_of_returning_garbage(tmp_path):
    import pytest
    with pytest.raises(Exception):
        load_rgb(tmp_path / "nope.jpg")
    (tmp_path / "bad.jpg").write_bytes(b"not an image")
    with pytest.raises(Exception):
        load_rgb(tmp_path / "bad.jpg")


# ----------------------------------------------------------------------- сторож: нигде в проекте нет «сырого» чтения

def _direct_opens(directory: str) -> list[str]:
    found = []
    for path in sorted((ROOT / directory).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "open"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in {"Image", "PILImage"}):
                if "exif-ok" not in lines[node.lineno - 1]:
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def test_project_code_never_opens_images_directly_without_the_exif_ok_marker():
    # Всё, что читает снимок или эталонное фото, обязано идти через load_rgb: иначе фото с телефона окажутся повёрнутыми.
    assert _direct_opens("app") + _direct_opens("scripts") + _direct_opens("ml") == []


def test_worker_reads_images_only_through_load_rgb():
    for rel in ("app/worker.py",):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "load_rgb(" in src and '.convert("RGB")' not in src, rel
    assert (ROOT / "app" / "worker.py").read_text(encoding="utf-8").count("load_rgb(") >= 3     # скачанное эталонное фото, фото при добавлении, кадр при распознавании


# ----------------------------------------------------------------------- общая вырезка кропа (пайплайн и оценка используют одну функцию)

def test_crop_box_rounds_clamps_and_returns_empty_for_inverted_or_outside_boxes():
    from app.pipeline.imageio import crop_box
    img = np.arange(10 * 20 * 3, dtype=np.uint8).reshape(10, 20, 3)
    assert crop_box(img, 2.4, 1.6, 7.5, 5.4).shape == (3, 6, 3)                       # X: 2..8 (round(7.5)=8), Y: 2..5 -> высота 3, ширина 6
    assert (crop_box(img, 0, 0, 20, 10) == img).all()
    assert crop_box(img, -50, -50, 5, 5).shape == (5, 5, 3) and crop_box(img, 15, 5, 500, 500).shape == (5, 5, 3)   # обрезка по кадру
    # малые отрицательные координаты: без явной обрезки срез Python трактовал бы их как отсчёт от конца и дал бы пустой кроп
    assert crop_box(img, -3, -2, 5, 5).shape == (5, 5, 3)
    assert crop_box(img, 8, 8, 4, 4).size == 0 and crop_box(img, 100, 100, 120, 120).size == 0


def test_evaluation_uses_the_shared_crop_function():
    # Конвейер аудита (app/pipeline/pipeline.py) удалён вместе с аудитом полок. Оценка точности по-прежнему режет товар общей
    # crop_box; когда распознавание станет YOLO -> crop -> DINOv2, identify должен резать ТЕМ ЖЕ crop_box (тест добавится там).
    assert "crop_box(rgb, x1, y1, x2, y2)" in (ROOT / "ml" / "evaluate_matching.py").read_text(encoding="utf-8")
