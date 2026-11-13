"""
Оффлайн-скрипт: пробегает по эталонным фото всех SKU каталога, считает
эмбеддинги, строит FAISS-индекс с нуля и сохраняет его на диск (index.save()) —
именно этот файл подхватывается воркером при старте (см. _load_or_create_index
в app/worker.py) и потому переживает перезапуски процесса.

Используется:
  - при первом разворачивании системы (полная пересборка индекса);
  - периодически (например, ночью) для консистентности, если добавление
    единичных SKU идёт "на лету" через CatalogService в течение дня.

Запуск: python -m ml.build_catalog_embeddings --catalog-dir data/catalog_reference_photos

ВАЖНО (2026-11-07): имена папок должны быть id товаров из базы — распознавание называет только товары, которые есть в базе.
Существующий рабочий индекс без --replace-existing не перезаписывается; с ним — сначала проверенная копия
(app/services/index_reconcile.prepare_full_rebuild). Для каталога из папок с артикулами используйте scripts/import_catalog.py.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


from app.core.config import get_settings
from app.pipeline.embedder import DinoV2Embedder
from app.pipeline.imageio import load_rgb
from app.pipeline.matcher import FaissCatalogIndex


def load_catalog_layout(catalog_dir: Path) -> dict[str, list[Path]]:
    """
    Ожидаемая структура каталога:
        catalog_dir/
            <product_id_1>/photo1.jpg, photo2.jpg, ...
            <product_id_2>/...
    """
    layout: dict[str, list[Path]] = {}
    for product_dir in sorted(catalog_dir.iterdir()):
        if not product_dir.is_dir():
            continue
        photos = sorted(list(product_dir.glob("*.jpg")) + list(product_dir.glob("*.png")))
        if photos:
            layout[product_dir.name] = photos
    return layout


def main() -> None:
    parser = argparse.ArgumentParser(description="Построение эмбеддингов каталога для векторного индекса")
    parser.add_argument("--catalog-dir", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, default=Path("models/index/build_manifest.json"))
    parser.add_argument(
        "--output-index", type=Path, default=None,
        help="Путь для сохранения индекса. По умолчанию — models.matcher.index_path из configs/config.yaml",
    )
    parser.add_argument("--replace-existing", action="store_true",
                        help="заменить существующий индекс (сначала будет сделана копия рядом с ним)")
    args = parser.parse_args()

    settings = get_settings()
    embedder_cfg = settings.embedder
    matcher_cfg = settings.matcher
    if embedder_cfg is None:
        raise SystemExit("В configs/config.yaml не задана секция models.embedder")

    output_index = args.output_index or (Path(matcher_cfg.index_path) if matcher_cfg else None)
    if output_index is None:
        raise SystemExit("Не задан путь сохранения индекса: передайте --output-index или models.matcher в config.yaml")
    from app.services.catalog_upload import manifest_path_for                 # noqa: PLC0415
    from app.services.index_reconcile import prepare_full_rebuild             # noqa: PLC0415
    backup = prepare_full_rebuild(str(output_index), args.replace_existing, manifest_path_for(output_index))  # ДО загрузки модели
    if backup:
        print(f"Копия прежнего индекса: {backup}")

    embedder = DinoV2Embedder(weights_path=embedder_cfg.weights_path, embedding_dim=embedder_cfg.embedding_dim)
    index = FaissCatalogIndex(embedding_dim=embedder_cfg.embedding_dim)

    layout = load_catalog_layout(args.catalog_dir)
    manifest: dict[str, int] = {}

    for product_id, photo_paths in layout.items():
        images = [load_rgb(p) for p in photo_paths]
        embeddings = embedder.embed(images)
        index.add(product_id, embeddings, [f"file:{p.parent.name}/{p.name}" for p in photo_paths])
        manifest[product_id] = len(photo_paths)
        print(f"[ok] {product_id}: {len(photo_paths)} эталонных фото -> эмбеддинги добавлены")

    args.output_manifest.parent.mkdir(parents=True, exist_ok=True)
    args.output_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    index.save(str(output_index))

    print(f"Готово: {len(manifest)} SKU проиндексировано.")
    print(f"  Манифест: {args.output_manifest}")
    print(f"  Индекс:   {output_index} (+ {output_index}.ids.json)")


if __name__ == "__main__":
    main()
