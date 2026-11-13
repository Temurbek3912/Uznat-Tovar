"""
Автоматическое размытие лиц людей, попавших в кадр при съёмке полки
(сотрудники, покупатели на заднем плане). Это не опциональная фича, а
обязательное требование раздела 3 ТЗ (privacy/комплаенс) — вызывается
в пайплайне ДО сохранения/показа фото кому-либо, включая до постановки
в очередь на ML-обработку, если хранится оригинал.

Реализация — Haar cascade из OpenCV: работает без скачивания весов
(файл каскада поставляется вместе с пакетом opencv-python), потому
пригодна как дефолт "из коробки". Для продакшна с более сложными ракурсами
(лицо не анфас, боковой свет) рекомендуется заменить на DNN-детектор лиц
(например, RetinaFace) — интерфейс `blur_faces()` не меняется.

Требует OpenCV 4.x. В 5.x (проверено на 5.0.0.93, 2026-11-08): ни одно из колёс opencv-python, -headless, -contrib-python не
содержит файлов каскадов (в cv2/data только __init__.py), а cv2.CascadeClassifier есть лишь в сборке contrib (модуль xobjdetect).
Размытие лиц НЕ отключается ни при каких
условиях: если каскад не загрузился, создание FaceBlurrer падает с понятным сообщением (см. app/core/opencv_check.py).
Путь к каскаду вычисляется при создании объекта, а не при импорте модуля: импорт не должен падать на «сломанном» cv2.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

FACE_CASCADE_FILE = "haarcascade_frontalface_default.xml"


class FaceBlurUnavailable(RuntimeError):
    """Размытие лиц невозможно (нет CascadeClassifier / файла каскада). Подкласс RuntimeError — прежний код его ловил."""


def default_cascade_path(cv2_module) -> str:
    data = getattr(cv2_module, "data", None)
    directory = getattr(data, "haarcascades", None)
    if not directory:
        raise FaceBlurUnavailable(
            f"в cv2 {getattr(cv2_module, '__version__', '?')} нет cv2.data.haarcascades — не найти файл каскада лиц. "
            "Нужен OpenCV >=4.9,<5 (pip install --force-reinstall --no-deps \"opencv-python-headless>=4.9,<5\"); "
            "проверка: python -m scripts.check_opencv"
        )
    return str(Path(directory) / FACE_CASCADE_FILE)


class FaceBlurrer:
    def __init__(self, cascade_path: str | None = None, blur_kernel: int = 51,
                 scale_factor: float = 1.1, min_neighbors: int = 5, cv2_module=None):
        if cv2_module is None:
            import cv2 as cv2_module  # noqa: PLC0415 — ленивый импорт: модуль можно импортировать без рабочего cv2
        self._cv2 = cv2_module
        version = getattr(cv2_module, "__version__", "?")
        if not hasattr(cv2_module, "CascadeClassifier"):
            raise FaceBlurUnavailable(
                f"в cv2 {version} нет атрибута CascadeClassifier (OpenCV 5.x его не содержит, либо две сборки opencv-python "
                "перезаписали друг друга). Размытие лиц обязательно и не отключается: нужен OpenCV >=4.9,<5 в единственной "
                "сборке opencv-python-headless. Проверка: python -m scripts.check_opencv"
            )
        path = cascade_path or default_cascade_path(cv2_module)
        if not Path(path).is_file():
            raise FaceBlurUnavailable(f"файл каскада для детекции лиц не найден: {path}")
        self._detector = cv2_module.CascadeClassifier(path)
        if self._detector.empty():
            raise FaceBlurUnavailable(f"Не удалось загрузить каскад для детекции лиц: {path}")
        self.cascade_path = path
        logger.info("FaceBlurrer: каскад %s загружен (cv2 %s)", path, version)

        # Ядро размытия должно быть нечётным — обеспечиваем это независимо от входного значения
        self.blur_kernel = blur_kernel if blur_kernel % 2 == 1 else blur_kernel + 1
        self.scale_factor = scale_factor
        self.min_neighbors = min_neighbors

    def blur_faces(self, image: np.ndarray) -> tuple[np.ndarray, int]:
        """
        Возвращает (изображение_с_размытыми_лицами, количество_найденных_лиц).
        Изображение не мутируется — возвращается копия.
        """
        cv2 = self._cv2
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
        faces = self._detector.detectMultiScale(
            gray, scaleFactor=self.scale_factor, minNeighbors=self.min_neighbors, minSize=(24, 24)
        )

        result = image.copy()
        for (x, y, w, h) in faces:
            # Небольшой запас вокруг рамки лица, чтобы размытие захватывало края (уши, подбородок)
            pad_x, pad_y = int(w * 0.15), int(h * 0.15)
            x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
            x2, y2 = min(image.shape[1], x + w + pad_x), min(image.shape[0], y + h + pad_y)

            roi = result[y1:y2, x1:x2]
            if roi.size == 0:
                continue
            result[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (self.blur_kernel, self.blur_kernel), 0)

        return result, len(faces)
