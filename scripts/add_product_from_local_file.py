"""
Быстрое добавление SKU в каталог с локальных файлов — без API, без воркера,
без необходимости выкладывать фото по URL (в отличие от продового пути через
POST /api/v1/catalog/products, который скачивает эталонные фото по сети).

Использует ровно те же компоненты (DinoV2Embedder, FaissCatalogIndex,
CatalogService), что и продовый путь — это не отдельная "игрушечная" логика,
а прямой вызов того же кода в одноразовом CLI-сценарии.

Запуск:
    python -m scripts.add_product_from_local_file --sku TEST-001 \
        --name "Тестовый товар" --brand DemoBrand --category "Тест" \
        --price 99.90 --photo path/to/photo1.jpg --photo path/to/photo2.jpg
"""
from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.core.schemas import ProductIn
from app.db.session import SessionLocal
from app.pipeline.embedder import DinoV2Embedder, model_signature
from app.pipeline.imageio import load_rgb
from app.pipeline.matcher import open_catalog_index
from app.services.catalog_service import CatalogService


def _load_index(index_path: str, embedding_dim: int, model: dict | None = None):
    """Тот же загрузчик, что у воркера: повреждённый индекс или индекс другой модели — остановка с причиной, а не пустой
    индекс поверх файла."""
    return open_catalog_index(index_path, embedding_dim, model)


def main() -> None:
    parser = argparse.ArgumentParser(description="Добавить SKU в каталог с локальных фото")
    parser.add_argument("--sku", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--brand", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--photo", action="append", required=True, help="Путь к фото; можно указать несколько раз")
    args = parser.parse_args()

    settings = get_settings()
    if settings.embedder is None or settings.matcher is None:
        raise SystemExit("В configs/config.yaml не заданы models.embedder / models.matcher")

    print("Загружаю модель эмбеддера (при первом запуске скачает веса из интернета)...")
    embedder = DinoV2Embedder(
        weights_path=settings.embedder.weights_path, embedding_dim=settings.embedder.embedding_dim,
    )
    index = _load_index(settings.matcher.index_path, settings.embedder.embedding_dim, model_signature(embedder))

    images = [load_rgb(p) for p in args.photo]
    product_in = ProductIn(
        sku=args.sku, name=args.name, brand=args.brand, category=args.category,
        expected_price=args.price, reference_image_urls=[str(p) for p in args.photo],
    )

    db = SessionLocal()
    try:
        service = CatalogService(db=db, embedder=embedder, index=index, index_path=settings.matcher.index_path)
        product = service.add_product(product_in, images)
        print(f"Готово: SKU {product.sku} -> product_id={product.id}")
        print(f"Индекс сохранён: {settings.matcher.index_path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
