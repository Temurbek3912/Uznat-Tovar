"""
Добавление SKU в каталог — без переобучения детектора и эмбеддера (раздел 5 ТЗ).

База данных — единственный источник истины о существующих товарах (2026-11-07; логика — app/services/catalog_add.py):
  1. посчитать эмбеддинги эталонных фото тем же ImageEmbedder, что в распознавании (до записи в базу);
  2. сохранить товар в базе (commit); товар с тем же артикулом уже есть — используется он (повтор безопасен);
  3. записать векторы в индекс и сразу сохранить индекс на диск.
Раньше шаги шли в обратном порядке: откат базы после сохранения индекса оставлял на диске векторы несуществующего товара.

persist=False — не сохранять индекс после добавления (пакетные сценарии сохраняют его один раз в конце).
"""
from __future__ import annotations

import numpy as np
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.schemas import ProductIn
from app.db.models import Product
from app.pipeline.embedder import ImageEmbedder
from app.pipeline.matcher import CatalogIndex
from app.services.catalog_add import DuplicateSku, add_product


class SqlProductStore:
    """ProductStore на SQLAlchemy: поиск по артикулу и создание с commit. Уникальность артикула — в базе (products.sku)."""

    def __init__(self, db: Session):
        self.db = db

    def find_id_by_sku(self, sku: str) -> str | None:
        row = self.db.query(Product.id).filter(Product.sku == sku).first()
        return row[0] if row else None

    def create(self, product_in: ProductIn) -> str:
        product = Product(sku=product_in.sku, name=product_in.name, brand=product_in.brand, category=product_in.category,
                          expected_price=product_in.expected_price)
        self.db.add(product)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()                     # сессия должна остаться пригодной: дальше ищем победителя гонки
            raise DuplicateSku(product_in.sku) from exc
        self.db.refresh(product)
        return product.id


class CatalogService:
    def __init__(self, db: Session, embedder: ImageEmbedder, index: CatalogIndex, index_path: str | None = None):
        self.db = db
        self.embedder = embedder
        self.index = index
        self.index_path = index_path

    def add_product(self, product_in: ProductIn, reference_images: list[np.ndarray], persist: bool = True) -> Product:
        """reference_images — уже загруженные и декодированные эталонные фото (в порядке product_in.reference_image_urls)."""
        if persist and self.index_path is None:
            raise RuntimeError(
                "CatalogService создан без index_path — некуда сохранять индекс. "
                "Либо передайте index_path в конструктор, либо вызывайте add_product(persist=False)."
            )
        outcome = add_product(product_in, reference_images, store=SqlProductStore(self.db), embedder=self.embedder,
                              index=self.index, index_path=self.index_path if persist else None)
        product = self.db.get(Product, outcome.product_id)
        if product is None:                        # товар только что был в базе; исчез — его удалили параллельно
            raise LookupError(f"товар {outcome.product_id} пропал из базы во время добавления")
        return product

    def save_index(self) -> None:
        if self.index_path is None:
            raise RuntimeError("CatalogService создан без index_path — некуда сохранять индекс.")
        self.index.save(self.index_path)
