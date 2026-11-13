"""
Единственное место, где изображения читаются в RGB-массив для пайплайна.

Зачем отдельный модуль. Телефон снимает полку в портретной ориентации, но пишет пиксели «как с датчика» (альбомно) и
сохраняет поворот в метаданных EXIF (тег Orientation). `Image.open(...).convert("RGB")` этот тег ИГНОРИРУЕТ, и портретный
снимок попадал бы в детектор повёрнутым на 90° или вверх ногами — полка «лежит на боку», товары не находятся. Браузер,
редакторы разметки (CVAT, LabelImg) и Ultralytics показывают/читают снимок уже повёрнутым, так что без этой нормализации
рамки разметки не совпали бы с тем, что видит наш код. Поэтому все чтения идут через `load_rgb`, а тест
(tests/test_imageio.py) запрещает прямой `Image.open(...)` в коде проекта без пометки `# exif-ok`.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


def load_rgb(source) -> np.ndarray:
    """Путь, байты или файловый объект -> RGB uint8 HxWx3 с применённым EXIF-поворотом."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(bytes(source))
    elif isinstance(source, Path):
        source = str(source)
    with Image.open(source) as im:  # exif-ok
        upright = ImageOps.exif_transpose(im)
        return np.array(upright.convert("RGB"))


def crop_box(image: np.ndarray, x1: float, y1: float, x2: float, y2: float) -> np.ndarray:
    """Вырезка по пиксельной рамке с округлением и обрезкой по кадру — ОДНА реализация для боевого пайплайна и для оценки
    (ml/evaluate_matching.py): подобранные пороги относятся к тем же кропам, что видит система."""
    x1, y1, x2, y2 = (int(round(v)) for v in (x1, y1, x2, y2))
    h, w = image.shape[:2]
    return image[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
