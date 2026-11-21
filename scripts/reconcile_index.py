"""
Сверка индекса визуального поиска с базой данных; по --apply — безопасное восстановление (app/services/index_reconcile.py).

    python -m scripts.reconcile_index            # только отчёт (ничего не меняет)
    python -m scripts.reconcile_index --apply    # убрать векторы товаров, которых нет в базе, и повторы одного фото;
                                                 # снять устаревшие отметки журнала
    python -m scripts.reconcile_index --verify-photos   # дополнительно проверить, не менялись ли фото после индексирования

Фото импорта каталога (data/catalog-imported) сверяются по записям о фото в базе: запись у этого товара, копия файла на месте,
содержимое то же (--verify-photos), у каждого активного эталона есть вектор. Недостающие векторы фото импорта воркер не
добавляет — их возвращает пересборка из базы: python -m scripts.rebuild_index --apply (модель не нужна, векторы из кэша базы).

--apply НЕ добавляет недостающие векторы (нужна модель): это делает воркер автоматически при каждом запуске
(задача recover_index) или вручную python -m scripts.reindex_uploads --apply.

Что делает --apply и чего НЕ делает:
  - сначала копирует файлы индекса и журнал в models/index/backup-<время>/ (копии сверяются по SHA-256) — копии не удаляются;
  - убирает только векторы с id, которых нет в базе; векторы товаров из базы остаются байт в байт;
  - фото и товары не трогает никогда;
  - если индекс в это время записал другой процесс (воркер, импорт) — отказ без изменений, запустите снова;
  - если «чужих» векторов больше 20 % или база пуста — отказ без изменений (вероятно, не та база или индекс от артикулов).
Товары без векторов, но с фото на диске, получают векторы командой: python -m scripts.reindex_uploads --apply
Воркер можно не останавливать: он перечитает изменённый индекс сам.
"""
from __future__ import annotations

import argparse


def _load_products() -> dict[str, str]:
    from app.db.models import Product           # noqa: PLC0415 — ленивые импорты: тесты подают товары сами
    from app.db.session import SessionLocal     # noqa: PLC0415
    db = SessionLocal()
    try:
        return {pid: sku for pid, sku in db.query(Product.id, Product.sku).all()}
    finally:
        db.close()


def _load_excluded() -> frozenset:
    """(товар, ссылка) фото, выведенных из эталонов или витринных (база — источник истины): их векторы не добавляются, а
    отсутствие вектора не считается пропуском (вектор выведенного фото убирает app/services/index_prune.py)."""
    from app.db.session import SessionLocal                       # noqa: PLC0415
    from app.services.product_images import PhotoRegistry         # noqa: PLC0415
    db = SessionLocal()
    try:
        return frozenset(PhotoRegistry(db).excluded_sources())
    finally:
        db.close()


def _load_import_photos() -> list:
    """Записи о фото импорта (product_images, источник «import:…») — для проверки векторов фото импорта."""
    from app.db.models import ProductImage                        # noqa: PLC0415
    from app.db.session import SessionLocal                       # noqa: PLC0415
    from app.services.index_coverage import ImportPhoto          # noqa: PLC0415
    from app.services.product_images import REFERENCE            # noqa: PLC0415
    db = SessionLocal()
    try:
        rows = (db.query(ProductImage.product_id, ProductImage.source_ref, ProductImage.storage_path, ProductImage.sha256,
                         ProductImage.role, ProductImage.is_active)
                .filter(ProductImage.source_ref.like("import:%")).all())
        return [ImportPhoto(str(pid), str(src), path or None, str(sha or ""), role == REFERENCE and bool(active))
                for pid, src, path, sha, role, active in rows]
    finally:
        db.close()


def main(argv: list[str] | None = None, out=print, load_products=None, settings=None, resolve_path=None,
         load_excluded=None, load_import_photos=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="выполнить восстановление (без флага — только отчёт)")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    ap.add_argument("--max-ghost-share", type=float, default=None, help="доля «чужих» векторов, выше которой отказ (по умолчанию 0.2)")
    ap.add_argument("--verify-photos", action="store_true", help="сверить содержимое фото с записанным при индексировании (читает все фото)")
    args = ap.parse_args(argv)

    from app.pipeline.matcher import open_catalog_index                  # noqa: PLC0415
    from app.services import index_reconcile as R                        # noqa: PLC0415
    from app.services import index_coverage as C                         # noqa: PLC0415
    from app.services.catalog_upload import folder_name, list_photo_files, manifest_path_for, upload_ref, uploads_root  # noqa: PLC0415

    if settings is None or resolve_path is None:
        from app.core.config import get_settings, resolve_project_path   # noqa: PLC0415
        settings = settings or get_settings()
        resolve_path = resolve_path or resolve_project_path
    if settings.matcher is None or settings.embedder is None:
        out("В configs/config.yaml не заданы models.embedder / models.matcher")
        return 2
    index_path = settings.matcher.index_path
    dim = settings.embedder.embedding_dim
    share = R.MAX_GHOST_SHARE if args.max_ghost_share is None else args.max_ghost_share
    products = (load_products or _load_products)()
    root = uploads_root(resolve_path(settings.media_root))
    journal = manifest_path_for(index_path)
    try:
        index = open_catalog_index(index_path, dim)
    except Exception as exc:  # noqa: BLE001 — непригодный индекс: сверка невозможна, причина человеку
        out(f"Индекс не загружается: {exc}")
        return 2
    report = R.analyze(index, products, photo_counts={sku: len(list_photo_files(root, sku)) for sku in products.values()},
                       journal_keys=R._load_journal_keys(journal), folder_of=folder_name, max_ghost_share=share)

    out(f"Индекс: {report.index_vectors} векторов, {report.index_products} товаров. В базе: {report.db_products} товаров.")
    out(f"Векторы товаров, которых нет в базе: {report.ghost_vectors} (товаров: {len(report.ghosts)})")
    for pid, n in sorted(report.ghosts.items())[:20]:
        out(f"  {pid}: {n}")
    out(f"Товары базы без векторов: {len(report.without_vectors)}; из них с фото на диске: {len(report.recoverable)}")
    for pid, sku in report.without_vectors[:20]:
        out(f"  {sku} ({pid})")
    out(f"Повторные векторы одного фото: {report.duplicate_vectors}")
    out(f"Устаревшие отметки журнала проиндексированных фото: {len(report.stale_journal_keys)}")
    coverage = C.analyze_coverage(index, products, root, folder_of=folder_name, list_photos=list_photo_files, upload_ref=upload_ref,
                                  manifest_keys=R._load_journal_keys(journal), verify_content=args.verify_photos,
                                  skip=(load_excluded or (_load_excluded if load_products is None else frozenset))(),
                                  import_photos=(load_import_photos or (_load_import_photos if load_products is None
                                                                        else (lambda: None)))(),
                                  media_root=resolve_path(settings.media_root))
    out("Полнота индекса (связь фото и векторов):")
    for line in C.describe(coverage):
        out(f"  {line}")
    for pid, sku, ref in coverage.photos_not_indexed[:20]:
        out(f"    без вектора: {ref} ({sku})")
    for pid, source in coverage.vectors_without_photo[:20]:
        out(f"    нет фото у вектора: {source} (товар {pid})")
    for pid, sku in coverage.products_without_vectors_or_photos[:20]:
        out(f"    товар без векторов и без фото — нужны новые фото: {sku} ({pid})")
    for pid, source in coverage.foreign_photo_vectors[:20]:
        out(f"    вектор фото другого товара: {source} (товар {pid})")
    for pid, source in coverage.changed_photos[:20]:
        out(f"    фото изменено после индексирования: {source} (товар {pid}) — вернуть фото из копии или заново добавить фото товара")
    for pid, source in coverage.import_vectors_without_record[:20]:
        out(f"    вектор фото импорта без записи о фото: {source} (товар {pid}) — запись добавит воркер при запуске")
    for pid, source in coverage.import_photos_not_indexed[:20]:
        out(f"    фото импорта без вектора: {source} (товар {pid})")
    for b in report.blockers:
        out(f"ОТКАЗ: {b}")
    if coverage.photos_not_indexed:
        out("Недостающие векторы добавит воркер при запуске или: python -m scripts.reindex_uploads --apply")
    if coverage.import_photos_not_indexed:
        out("Векторы фото импорта вернёт пересборка из базы: python -m scripts.rebuild_index (план), затем --apply")
    if report.clean:
        out("Лишних векторов и устаревших отметок нет." if not coverage.complete else "Расхождений нет.")
        return 0
    if not args.apply:
        out("\nЭто только отчёт. Восстановление: python -m scripts.reconcile_index --apply")
        return 1 if report.blockers else 0
    if report.blockers:
        return 2
    if not args.yes and input("Выполнить восстановление (копия будет сделана до изменений)? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
        out("Отменено.")
        return 1
    try:
        res = R.apply(index_path, dim, products, journal_path=journal if journal.is_file() else None, folder_of=folder_name,
                      max_ghost_share=share, out=out)
    except Exception as exc:  # noqa: BLE001
        out(f"Восстановление не выполнено: {exc}")
        return 2
    out(f"Готово: убрано векторов {res.removed_vectors}, осталось {res.kept_vectors}; снято отметок журнала {res.journal_keys_removed}; "
        f"проверка {'пройдена' if res.verified else 'НЕ пройдена'}. Копия прежних файлов: {res.backup_dir}")
    if report.recoverable:
        out("Добавить векторы товарам без них: python -m scripts.reindex_uploads --apply")
    return 0 if res.verified else 2


if __name__ == "__main__":
    raise SystemExit(main())
