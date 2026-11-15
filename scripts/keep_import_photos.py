"""
Копии фото уже загруженного каталога — внутрь программы (data/catalog-imported), чтобы покупатель видел фото в списке товаров
(frontend/shop-list.html), а сотрудник — на странице «Товары». Нужна для каталогов, загруженных до 2026-11-15: с этого дня
scripts/import_catalog.py кладёт копии сам.

    python -m scripts.keep_import_photos --dir data/test-catalog-uz            # проверка: что будет скопировано (ничего не меняет)
    python -m scripts.keep_import_photos --dir data/test-catalog-uz --apply    # скопировать и записать путь в базу

Что делает: для каждой записи о фото импорта без файла в программе (source_ref «import:…», storage_path пуст) ищет файл
<папка>/<артикул>/<имя> (как записано при загрузке), сверяет SHA-256 с записью и копирует байт в байт. Папку импорта не
трогает, векторы и индекс не меняет — на узнавание товара не влияет. Повторный запуск безопасен.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def plan_and_apply(db, folder: Path, media_root: Path, apply: bool) -> dict:
    from app.db.models import Product, ProductImage
    from app.services.catalog_upload import folder_name
    from app.services.product_images import keep_import_copy, sha256_file

    folder = Path(folder).resolve()
    stats = {"found": 0, "copied": 0, "missing": 0, "mismatch": 0}
    rows = (db.query(ProductImage, Product.sku).join(Product, Product.id == ProductImage.product_id)
            .filter(ProductImage.storage_path.is_(None)).filter(ProductImage.source_ref.like("import:%")).all())
    for image, sku in rows:
        name = (image.original_name or "").replace("\\", "/")
        source = (folder / name).resolve() if name else None
        if source is None or not source.is_relative_to(folder) or not source.is_file():
            stats["missing"] += 1
            continue
        stats["found"] += 1
        if not apply:
            if sha256_file(source) != image.sha256:
                stats["mismatch"] += 1
            continue
        rel = keep_import_copy(media_root, folder_name(sku), source, image.sha256)
        if rel is None:
            stats["mismatch"] += 1
            continue
        image.storage_path = rel
        stats["copied"] += 1
    if apply:
        db.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Копии фото загруженного каталога — в программу (для показа покупателю)")
    ap.add_argument("--dir", required=True, help="папка, из которой загружали каталог (scripts/import_catalog.py --dir)")
    ap.add_argument("--apply", action="store_true", help="скопировать и записать (без него — только проверка)")
    args = ap.parse_args(argv)
    folder = Path(args.dir)
    if not folder.is_dir():
        print(f"Нет папки: {folder}")
        return 2
    from app.core.config import get_settings, resolve_project_path
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        stats = plan_and_apply(db, folder, resolve_project_path(get_settings().media_root), args.apply)
    finally:
        db.close()
    if args.apply:
        print(f"Скопировано фото: {stats['copied']}; файл не совпал с записью: {stats['mismatch']}; нет файла в папке: {stats['missing']}.")
    else:
        print(f"Можно скопировать фото: {stats['found'] - stats['mismatch']}; файл не совпал с записью: {stats['mismatch']}; "
              f"нет файла в папке: {stats['missing']}. Ничего не изменено — для копирования добавьте --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
