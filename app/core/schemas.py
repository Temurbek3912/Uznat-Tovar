"""
Доменные и API-схемы. Разделены логически от SQLAlchemy ORM-моделей (app/db/models.py) —
это позволяет пайплайну работать с чистыми данными, не зная про БД.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DetectionStatus(str, Enum):
    MATCHED = "matched"          # уверенно распознан конкретный SKU
    UNCERTAIN = "uncertain"      # похож на несколько SKU, нужна ручная проверка
    UNKNOWN = "unknown"          # обнаружен объект, но это не товар из каталога


class BoundingBox(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


class ProductIn(BaseModel):
    """Данные для добавления/обновления SKU в каталоге (без переобучения моделей)."""

    sku: str
    name: str
    brand: str
    category: str
    expected_price: float | None = None  # справочная цена, показывается в карточке товара
    reference_image_urls: list[str] = Field(min_length=1)
    barcodes: list[str] = Field(default_factory=list)  # нормализованные штрих-коды (app/services/barcode.py); привязываются воркером после создания товара
