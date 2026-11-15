"""
Массовая загрузка каталога из папки: фото эталонов каждого товара -> эмбеддинги -> индекс + карточки товаров в БД.

    catalog/                         <- папка (любое имя)
        products.csv                 <- необязательно: sku;name;brand;category;price  (можно русские заголовки: Артикул;Название;...)
        COLA-0.5L/  1.jpg 2.jpg ...  <- папка = артикул (SKU), внутри 5–8 фото товара с разных сторон
        FANTA-0.5L/ 1.jpg ...

    # 1) Проверить папку БЕЗ базы и без моделей (ничего не пишет, нужен только Python и Pillow/OpenCV):
    python -m scripts.import_catalog --dir catalog --validate-only

    # 2) Загрузить:
    python -m scripts.import_catalog --dir catalog
    python -m scripts.import_catalog --dir catalog --update-metadata     # обновить названия/цены из CSV у существующих товаров

Повторный запуск безопасен: уже загруженные фото пропускаются (манифест по хешу файла), новые фото к существующему товару
добавляются. Обрыв (Ctrl+C, сбой питания) не портит данные: прогресс сохраняется, следующий запуск продолжит.
Полная пересборка индекса: удалить файлы индекса и манифест (см. docs/catalog_guide.md) и запустить снова.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.services.catalog_import import (
    ExistingProduct, Manifest, ProductMeta, build_plan, catalog_report, check_photo, decode_products_csv, format_catalog_report,
    parse_products_csv, run_import, scan_catalog_folder,
)

DEFAULT_MANIFEST = "models/index/import_manifest.json"
MAX_LISTED = 30


class SqlCatalogRepo:
    """Реальный репозиторий товаров на SQLAlchemy. Каждый товар фиксируется отдельным commit: сбой одного не откатывает остальные."""

    def __init__(self, db, media_root=None):
        self.db = db
        self.media_root = media_root            # есть — фото импорта копируются в программу (data/catalog-imported)

    def existing_products(self) -> dict[str, ExistingProduct]:
        from app.db.models import Product
        return {p.sku: ExistingProduct(p.id, p.name, p.brand, p.category, p.expected_price) for p in self.db.query(Product).all()}

    def create_product(self, meta: ProductMeta) -> str:
        from app.db.models import Product
        product = Product(sku=meta.sku, name=meta.name, brand=meta.brand, category=meta.category, expected_price=meta.expected_price)
        try:
            self.db.add(product)
            self.db.commit()
            self.db.refresh(product)
        except Exception:
            self.db.rollback()                      # сессия должна остаться пригодной для следующих товаров
            raise
        return product.id

    def update_product(self, product_id: str, meta: ProductMeta) -> None:
        from app.db.models import Product
        product = self.db.get(Product, product_id)
        if product is None:
            raise LookupError(f"товар {product_id} не найден")
        product.name, product.brand, product.category, product.expected_price = meta.name, meta.brand, meta.category, meta.expected_price
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def remember_photos(self, product_id: str, sku: str, photos, vectors, signature: dict | None) -> None:
        """Записи о фото (product_images) и векторы в кэш (image_embeddings). Источник вектора — «import:<SHA-256>».
        Копия фото кладётся в программу (media_root/catalog-imported, байт в байт) — так фото видно в списке товаров
        покупателя и на странице «Товары», даже если папку импорта потом удалят. Копия не удалась — фото всё равно
        записывается (без пути хранения, как раньше)."""
        from app.services.catalog_upload import folder_name
        from app.services.product_images import PhotoInfo, PhotoRegistry, keep_import_copy

        def stored(c):
            if self.media_root is None:
                return None
            try:
                return keep_import_copy(self.media_root, folder_name(sku), c.path, c.sha256)
            except OSError:
                return None
        infos = [PhotoInfo(sha256=c.sha256, source_ref=f"import:{c.sha256}", original_name=f"{sku}/{c.path.name}",
                           storage_path=stored(c), width=c.width or None, height=c.height or None) for c in photos]
        try:
            PhotoRegistry(self.db).remember(product_id, infos, vectors, signature)
        except Exception:
            self.db.rollback()                      # сессия должна остаться пригодной для следующих товаров
            raise


def _print_limited(out, title: str, items: list[str]) -> None:
    if not items:
        return
    out(f"\n{title} ({len(items)}):")
    for line in items[:MAX_LISTED]:
        out(f"  - {line}")
    if len(items) > MAX_LISTED:
        out(f"  … и ещё {len(items) - MAX_LISTED}")


def prepare(directory: Path, csv_path: Path | None, out=print):
    """Сканирование + CSV + проверка каждого фото. -> (папки, метаданные, проверки, предупреждения, ошибки)."""
    folders, warnings, errors = scan_catalog_folder(directory)
    csv_path = csv_path or (directory / "products.csv" if (directory / "products.csv").is_file() else None)
    metas: dict[str, ProductMeta] = {}
    if csv_path is not None:
        if not csv_path.is_file():
            errors.append(f"нет файла {csv_path}")
        else:
            try:
                text, csv_warnings = decode_products_csv(csv_path.read_bytes())
            except ValueError as exc:
                text, csv_warnings = "", []
                errors.append(str(exc))
            warnings += csv_warnings
            if text:
                metas, csv_errors = parse_products_csv(text)
                errors += [f"products.csv: {e}" for e in csv_errors]
    else:
        warnings.append("products.csv не найден — у всех товаров название = артикул, бренд и категория по умолчанию")
    total = sum(len(f.photos) for f in folders)
    out(f"Найдено товаров: {len(folders)}, фото: {total}. Проверяю фото ...")
    checks = {p: check_photo(p) for f in folders for p in f.photos}
    return folders, metas, checks, warnings, errors


def validate_only(args, out=print) -> int:
    folders, metas, checks, warnings, errors = prepare(Path(args.dir), Path(args.csv) if args.csv else None, out)
    plan = build_plan(folders, metas, Manifest(), checks, require_csv=args.require_csv)
    warnings += plan.warnings
    errors += plan.errors
    usable = sum(len(s.new_photos) for s in plan.skus)
    _print_limited(out, "ПРЕДУПРЕЖДЕНИЯ", warnings)
    _print_limited(out, "ОШИБКИ", errors)
    out(f"\nТоваров: {len(plan.skus)}, пригодных фото: {usable}.")
    if errors:
        out("Исправьте ошибки и запустите снова.")
        return 1
    out("Папка готова к загрузке: уберите --validate-only." if usable else "Пригодных фото нет — загружать нечего.")
    return 0 if usable else 1


def main(argv: list[str] | None = None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="папка каталога (подпапка на каждый артикул)")
    ap.add_argument("--csv", help="products.csv; по умолчанию <dir>/products.csv, если есть")
    ap.add_argument("--validate-only", action="store_true", help="только проверить папку; без базы и моделей")
    ap.add_argument("--require-csv", action="store_true", help="ошибка, если у какой-то папки нет строки в products.csv")
    ap.add_argument("--update-metadata", action="store_true", help="обновить название/бренд/категорию/цену у существующих товаров из CSV")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST, help=f"манифест загруженных фото (по умолчанию {DEFAULT_MANIFEST})")
    ap.add_argument("--checkpoint", type=int, default=25, help="сохранять индекс после каждых N товаров (по умолчанию 25)")
    ap.add_argument("--batch", type=int, default=16, help="сколько фото считать за один проход модели (по умолчанию 16)")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    args = ap.parse_args(argv)

    if args.validate_only:
        return validate_only(args, out)

    folders, metas, checks, warnings, errors = prepare(Path(args.dir), Path(args.csv) if args.csv else None, out)

    from app.core.config import get_settings                        # ленивые импорты: для --validate-only они не нужны
    from app.db.session import SessionLocal
    from app.pipeline.embedder import DinoV2Embedder
    from scripts.add_product_from_local_file import _load_index

    settings = get_settings()
    if settings.embedder is None or settings.matcher is None:
        out("В configs/config.yaml не заданы models.embedder / models.matcher")
        return 2
    manifest_path = Path(args.manifest)
    manifest = Manifest.load(manifest_path)
    db = SessionLocal()
    try:
        from app.core.config import resolve_project_path  # noqa: PLC0415
        repo = SqlCatalogRepo(db, media_root=resolve_project_path(settings.media_root))
        plan = build_plan(folders, metas, manifest, checks, repo.existing_products(), require_csv=args.require_csv)
        warnings += plan.warnings
        errors += plan.errors
        _print_limited(out, "ПРЕДУПРЕЖДЕНИЯ", warnings)
        _print_limited(out, "ОШИБКИ", errors)
        if errors:
            out("\nЗагрузка не начата: исправьте ошибки (--validate-only покажет их без базы).")
            return 1
        out(f"\nПлан: создать {plan.count('create')}, дополнить фото {plan.count('add_photos')}, без изменений {plan.count('unchanged')}, "
            f"пропустить {plan.count('skip')}; новых фото: {sum(len(s.new_photos) for s in plan.skus)}")
        if not any(s.action in ('create', 'add_photos') or (s.meta_changed and args.update_metadata) for s in plan.skus):
            out("Нечего загружать: всё уже в каталоге.")
            return 0
        if not args.yes and input("Продолжить? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
            out("Отменено.")
            return 1
        out("Загружаю модель эмбеддера (при первом запуске скачает веса из интернета)...")
        embedder = DinoV2Embedder(weights_path=settings.embedder.weights_path, embedding_dim=settings.embedder.embedding_dim)
        from app.pipeline.embedder import model_signature  # noqa: PLC0415
        index = _load_index(settings.matcher.index_path, settings.embedder.embedding_dim, model_signature(embedder))
        res = run_import(plan, repo, embedder, index, index_path=settings.matcher.index_path, manifest=manifest,
                         manifest_path=manifest_path, update_metadata=args.update_metadata, batch_size=args.batch,
                         checkpoint_every=args.checkpoint, out=out,
                         remember=lambda pid, sku, photos, emb: repo.remember_photos(pid, sku, photos, emb,
                                                                                     model_signature(embedder)))
    finally:
        db.close()

    out(f"\nГотово: создано {res.created}, дополнено {res.extended}, без изменений {res.unchanged}, пропущено {res.skipped}; "
        f"фото добавлено {res.photos_added}; данные обновлены {res.metadata_updated}.")
    if res.failed:
        _print_limited(out, "СБОИ (остальные товары загружены; повторный запуск догрузит эти)", res.failed)
    out("\n" + format_catalog_report(catalog_report(res.embeddings)))
    out(f"\nИндекс: {settings.matcher.index_path}   Манифест: {manifest_path}")
    return 1 if res.failed or res.interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
