"""
Опытный индекс из папки с фото — для сравнения моделей и проверок, НЕ для рабочего индекса.

    python -m ml.build_catalog_embeddings --catalog-dir <папка> --output-index D:/опыты/catalog.faiss

Структура папки: <папка>/<id товара или артикул>/1.jpg, 2.png ... Каждая папка сопоставляется с товаром базы — по id, иначе
по артикулу (с учётом регистра, как в базе). Хоть одна папка не совпала — отказ ДО загрузки модели и до записи, со списком
папок: такой индекс ничего бы не распознавал (распознавание называет только товары базы).

Рабочий индекс (models.matcher.index_path) и облегчённый (data/lite/index) этот инструмент не пишет (с 2026-11-21): он берёт
фото мимо базы — записей product_images и векторов в кэше у них нет, и полная замена выбросила бы векторы фото импорта и
загруженных из браузера. Для рабочего индекса:
  - новые товары или фото из папок с артикулами — python -m scripts.import_catalog --dir <папка> (товары, фото, векторы);
  - пересобрать рабочий индекс целиком — python -m scripts.rebuild_index --apply (из базы и кэша векторов).
Существующий опытный индекс без --replace-existing не перезаписывается; с ним — сначала проверенная копия рядом
(app/services/index_reconcile.prepare_full_rebuild). Список товаров и числа фото — в <индекс>.build_manifest.json.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

MAX_LISTED = 30
REDIRECT = ("Для рабочего индекса: товары и фото из папок с артикулами — python -m scripts.import_catalog --dir <папка>; "
            "пересобрать рабочий индекс из базы — python -m scripts.rebuild_index --apply.")


def load_catalog_layout(catalog_dir: Path) -> dict[str, list[Path]]:
    """Папки с фото (.jpg/.png): имя папки -> фото."""
    layout: dict[str, list[Path]] = {}
    for product_dir in sorted(catalog_dir.iterdir()):
        if not product_dir.is_dir():
            continue
        photos = sorted(list(product_dir.glob("*.jpg")) + list(product_dir.glob("*.png")))
        if photos:
            layout[product_dir.name] = photos
    return layout


def match_folders(folders, products: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Имя папки -> id товара: сначала как id, затем как артикул. Возвращает (сопоставленные, неизвестные)."""
    by_sku = {sku: pid for pid, sku in products.items() if sku}
    matched: dict[str, str] = {}
    unknown: list[str] = []
    for name in folders:
        pid = name if name in products else by_sku.get(name)
        if pid is None:
            unknown.append(name)
        else:
            matched[name] = pid
    return matched, unknown


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _load_products() -> dict[str, str]:
    from app.db.models import Product           # noqa: PLC0415 — ленивые импорты: тесты подают товары сами
    from app.db.session import SessionLocal     # noqa: PLC0415
    db = SessionLocal()
    try:
        return {pid: sku for pid, sku in db.query(Product.id, Product.sku).all()}
    finally:
        db.close()


def _load_embedder(embedder_cfg):
    from app.pipeline.embedder import create_embedder  # noqa: PLC0415
    return create_embedder(embedder_cfg)


def _load_image(path: Path):
    from app.pipeline.imageio import load_rgb  # noqa: PLC0415
    return load_rgb(path)


def main(argv: list[str] | None = None, out=print, settings=None, load_products=None, make_embedder=None,
         load_image=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog-dir", type=Path, required=True)
    parser.add_argument("--output-index", type=Path, default=None,
                        help="куда записать опытный индекс (обязательно; рабочий путь не принимается)")
    parser.add_argument("--output-manifest", type=Path, default=None,
                        help="список товаров и числа фото (по умолчанию — <индекс>.build_manifest.json)")
    parser.add_argument("--replace-existing", action="store_true",
                        help="заменить существующий опытный индекс по этому пути (сначала будет сделана копия рядом с ним)")
    args = parser.parse_args(argv)

    from app.core.config import PROJECT_ROOT, resolve_project_path            # noqa: PLC0415
    if settings is None:
        from app.core.config import get_settings                              # noqa: PLC0415
        settings = get_settings()
    if settings.embedder is None:
        out("В configs/config.yaml не задана секция models.embedder")
        return 2
    if args.output_index is None:
        out("Не задан --output-index. Этот инструмент строит только опытный индекс в отдельный путь. " + REDIRECT)
        return 2
    output_index = resolve_project_path(str(args.output_index))
    protected = [PROJECT_ROOT / "data" / "lite" / "index" / "catalog.faiss"]
    if settings.matcher is not None:
        protected.insert(0, resolve_project_path(settings.matcher.index_path))
    if any(_same_path(output_index, p) for p in protected):
        out(f"Отказ: {output_index} — индекс, которым пользуется программа. Этот инструмент берёт фото мимо базы (без записей "
            "о фото и кэша векторов) и заменил бы индекс целиком. " + REDIRECT)
        return 2
    if not args.catalog_dir.is_dir():
        out(f"Нет папки {args.catalog_dir}")
        return 2

    layout = load_catalog_layout(args.catalog_dir)
    if not layout:
        out(f"В {args.catalog_dir} нет папок с фото (.jpg/.png) — нечего индексировать.")
        return 2
    products = (load_products or _load_products)()
    matched, unknown = match_folders(layout, products)
    if unknown:
        out(f"Отказ: папок, которых нет в базе ни как id товара, ни как артикул: {len(unknown)} из {len(layout)}. Индекс с ними "
            "ничего бы не распознавал (распознавание называет только товары базы). Ничего не записано.")
        for name in unknown[:MAX_LISTED]:
            out(f"  неизвестная папка: {name}")
        if len(unknown) > MAX_LISTED:
            out(f"  … и ещё {len(unknown) - MAX_LISTED}")
        out("Если это новые товары (папки с артикулами) — загрузите их: python -m scripts.import_catalog --dir "
            f"{args.catalog_dir} (сначала проверка: --validate-only).")
        return 1
    by_sku = sum(1 for name, pid in matched.items() if name != pid)
    if by_sku:
        out(f"Папок, сопоставленных по артикулу: {by_sku}")

    from app.services.catalog_upload import manifest_path_for                 # noqa: PLC0415
    from app.services.index_reconcile import prepare_full_rebuild             # noqa: PLC0415
    try:
        backup = prepare_full_rebuild(str(output_index), args.replace_existing, manifest_path_for(output_index))  # ДО модели
    except SystemExit as exc:
        out(str(exc))
        return 1
    if backup:
        out(f"Копия прежнего индекса: {backup}")

    from app.pipeline.embedder import model_signature                         # noqa: PLC0415
    from app.pipeline.matcher import FaissCatalogIndex                        # noqa: PLC0415
    embedder = (make_embedder or _load_embedder)(settings.embedder)
    index = FaissCatalogIndex(embedding_dim=settings.embedder.embedding_dim)
    index.model = model_signature(embedder)               # все векторы — этой моделью (паспорт модели индекса)
    load = load_image or _load_image
    manifest: dict[str, int] = {}
    for folder, photo_paths in layout.items():
        product_id = matched[folder]
        embeddings = embedder.embed([load(p) for p in photo_paths])
        index.add(product_id, embeddings, [f"file:{folder}/{p.name}" for p in photo_paths])
        manifest[product_id] = manifest.get(product_id, 0) + len(photo_paths)
        out(f"[ok] {folder}" + (f" (товар {product_id})" if folder != product_id else "") + f": {len(photo_paths)} фото")

    index.save(str(output_index))
    manifest_out = args.output_manifest or Path(f"{output_index}.build_manifest.json")
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    out(f"Готово: товаров {len(manifest)}, фото {sum(manifest.values())}.")
    out(f"  Индекс:   {output_index}")
    out(f"  Манифест: {manifest_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
