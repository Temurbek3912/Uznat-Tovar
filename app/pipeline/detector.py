"""
Детектор товарных позиций на фото полки.

Архитектурное решение: детектор class-agnostic — он находит рамки объектов
класса "товар", не пытаясь угадать конкретный SKU. Распознавание SKU —
отдельная задача (см. embedder.py + matcher.py). Так каталог можно обновлять
без переобучения детектора.

Тяжёлая ML-библиотека (ultralytics) импортируется лениво (внутри __init__),
чтобы модуль можно было импортировать и тестировать в окружениях без
установленного torch/ultralytics (например, в unit-тестах бизнес-логики).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.core.schemas import BoundingBox


@dataclass
class RawDetection:
    bbox: BoundingBox
    confidence: float


class ProductDetector(Protocol):
    def detect(self, image: np.ndarray) -> list[RawDetection]:
        ...


class YoloShelfDetector:
    """Продакшн-реализация поверх Ultralytics YOLO."""

    def __init__(self, weights_path: str, confidence_threshold: float = 0.35, iou_threshold: float = 0.5,
                 max_detections: int = 1000):
        # Файл проверяем САМИ: Ultralytics при несуществующем пути с «известным» именем (yolov8n.pt …) молча пытается скачать
        # другую модель из интернета, а при неизвестном — падает невнятной ошибкой. Нам нужна именно эта модель.
        from pathlib import Path

        if not Path(weights_path).is_file():
            raise FileNotFoundError(f"файл весов детектора не найден: {weights_path}")
        try:
            from ultralytics import YOLO  # локальный импорт: тяжёлая зависимость
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "ultralytics не установлен. Установите зависимости из requirements.txt, "
                "чтобы использовать YoloShelfDetector."
            ) from exc

        self._model = YOLO(weights_path)
        task = getattr(self._model, "task", "detect")
        if task != "detect":
            raise RuntimeError(f"модель {weights_path} решает задачу «{task}», а детектору нужна «detect»")
        self.weights_path = weights_path
        self.confidence_threshold = confidence_threshold
        self.iou_threshold = iou_threshold
        self.max_detections = max_detections

    def detect(self, image: np.ndarray) -> list[RawDetection]:
        results = self._model.predict(
            image,
            conf=self.confidence_threshold,
            iou=self.iou_threshold,
            max_det=self.max_detections,
            verbose=False,
        )
        detections: list[RawDetection] = []
        for result in results:
            for box in result.boxes:
                x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
                conf = float(box.conf[0])
                detections.append(
                    RawDetection(bbox=BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2), confidence=conf)
                )
        return detections


def _area(b: tuple[float, float, float, float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inside_fraction(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> float:
    """Какая доля площади `inner` лежит внутри `outer`."""
    ix = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    iy = max(0.0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    a = _area(inner)
    return (ix * iy) / a if a > 0 else 0.0


def suppress_nested_boxes(boxes: list[tuple[float, float, float, float]], contain: float = 0.8) -> list[tuple[float, float, float, float]]:
    """Убирает вложенные рамки. Правило: если рамка содержит две и больше других — это «полость»/фон (стенки стеллажа,
    область между досками), её выбрасываем, а содержимое оставляем; если содержит ровно одну — это упаковка с этикеткой внутри,
    выбрасываем внутреннюю. O(n^2): число кандидатов ограничено сверху."""
    n = len(boxes)
    areas = [_area(b) for b in boxes]
    # j содержит i: i лежит в j (>= contain по площади) и j больше (при равенстве — по индексу, чтобы дубликаты не выбили друг друга)
    containers: list[list[int]] = [[] for _ in range(n)]
    inside_count = [0] * n
    for i in range(n):
        for j in range(n):
            if i != j and (areas[j] > areas[i] or (areas[j] == areas[i] and j < i)) and _inside_fraction(boxes[i], boxes[j]) >= contain:
                containers[i].append(j)
                inside_count[j] += 1
    keep = []
    for i in range(n):
        if inside_count[i] >= 2:
            continue                                   # полость/фон с несколькими рамками внутри
        if any(inside_count[j] < 2 for j in containers[i]):
            continue                                   # этикетка/элемент внутри упаковки
        keep.append(boxes[i])
    return keep


class HeuristicContourDetector:
    """
    Лёгкая CV-заглушка без ML-весов: ищет упаковки как замкнутые области между линиями границ (Canny).

    Полезна для (а) черновой предразметки перед ручной правкой, (б) демо на ПРОСТОМ макете (однотонные коробки на светлом
    фоне), (в) unit-тестов пайплайна на синтетических изображениях, (г) fallback при отсутствии обученных весов.
    НЕ предназначена для реальных полок. Замеры на синтетических полках (цветные коробки на светлом фоне, 3 яруса x 6, IoU 0.5):
    с зазорами 14 px F1 ≈ 66–79%, плотно (зазор 3 px) ≈ 71–79%, вплотную без зазора ≈ 32%. Принципиальные пределы подхода:
    товар без видимой границы с доской полки (тёмный на тёмном) сливается с ней и с соседями; на фото с богатой текстурой
    появляются лишние рамки. Для реальной работы нужен обученный YOLO (docs/detector_quickstart.md).

    Прежняя версия брала внешние контуры линий и склеивала весь ярус полки в одну рамку: F1 = 0% даже на простой синтетике.
    """

    MAX_CANDIDATES = 300      # потолок кандидатов после фильтров (защита от O(n^2) на шумных фото)

    def __init__(self, min_area_px: int = 400, max_area_ratio: float = 0.25, min_extent: float = 0.5,
                 min_aspect: float = 0.15, max_aspect: float = 8.0, pad: int = 2):
        self.min_area_px = min_area_px
        self.max_area_ratio = max_area_ratio      # рамка больше этой доли кадра — это фон/стеллаж, а не товар
        self.min_extent = min_extent              # доля площади рамки, реально занятая областью (отсекает «рваные» формы)
        self.min_aspect, self.max_aspect = min_aspect, max_aspect      # допустимое отношение высота/ширина
        self.pad = pad                            # граница обводки съедает пару пикселей — возвращаем

    def detect(self, image: np.ndarray) -> list[RawDetection]:
        import cv2

        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
        height, width = gray.shape[:2]
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        edges = cv2.dilate(cv2.Canny(gray, 50, 150), np.ones((3, 3), np.uint8), iterations=1)
        # RETR_CCOMP: нужны «дыры» — области, замкнутые линиями (внутренность упаковки), а не внешние контуры самих линий.
        contours, hierarchy = cv2.findContours(edges, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None:
            return []

        candidates: list[tuple[float, float, float, float]] = []
        for i, cnt in enumerate(contours):
            if hierarchy[0][i][3] == -1:
                continue                                              # внешний контур линии, не область
            x, y, w, h = cv2.boundingRect(cnt)
            area = w * h
            if area < self.min_area_px or area > self.max_area_ratio * height * width:
                continue
            if not (self.min_aspect <= h / max(w, 1) <= self.max_aspect):
                continue
            if cv2.contourArea(cnt) < self.min_extent * area:
                continue
            candidates.append((float(x - self.pad), float(y - self.pad), float(x + w + self.pad), float(y + h + self.pad)))

        candidates = sorted(candidates, key=_area, reverse=True)[: self.MAX_CANDIDATES]
        detections: list[RawDetection] = []
        for x1, y1, x2, y2 in suppress_nested_boxes(candidates):
            detections.append(
                RawDetection(
                    bbox=BoundingBox(x1=max(0.0, x1), y1=max(0.0, y1), x2=min(float(width), x2), y2=min(float(height), y2)),
                    confidence=0.5,  # эвристика не даёт калиброванной уверенности
                )
            )
        return detections
