"""
Записи о существующих фото товаров в базе (product_images) и перенос векторов индекса в кэш (image_embeddings).

Фото, добавленные до 2026-11-14, лежат файлами (data/catalog-uploads/<папка товара>/…) или известны только индексу (импорт
каталога: «import:<SHA-256>»). Скрипт создаёт записи о них в базе. ФАЙЛЫ НЕ УДАЛЯЮТСЯ И НЕ ПЕРЕМЕЩАЮТСЯ.
Векторы индекса переносятся в кэш, только если индекс знает свою модель (манифест индекса, 2026-11-14); векторы старого
индекса неизвестной модели не переносятся — для них нужна полная пересборка моделью.

Воркер делает то же самое сам при каждом запуске; скрипт — чтобы посмотреть или выполнить вручную. Повтор безопасен.

    python -m scripts.register_photos            # только показать (ничего не меняет)
    python -m scripts.register_photos --apply    # записать
"""
from __future__ import annotations

import argparse


def _open_session():
    from app.db.session import SessionLocal     # noqa: PLC0415 — ленивые импорты: тесты подают сессию сами
    return SessionLocal()


def main(argv: list[str] | None = None, out=print, settings=None, resolve_path=None, make_session=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="записать в базу (без флага — только показать)")
    args = ap.parse_args(argv)

    from app.db.models import Product                                                       # noqa: PLC0415
    from app.pipeline.matcher import open_catalog_index                                     # noqa: PLC0415
    from app.services.catalog_upload import folder_name, list_photo_files, upload_ref, uploads_root  # noqa: PLC0415
    from app.services.product_images import PhotoRegistry, backfill                         # noqa: PLC0415
    if settings is None or resolve_path is None:
        from app.core.config import get_settings, resolve_project_path                     # noqa: PLC0415
        settings = settings or get_settings()
        resolve_path = resolve_path or resolve_project_path
    root = uploads_root(resolve_path(settings.media_root))
    index = None
    if settings.matcher is not None and settings.embedder is not None:
        try:
            index = open_catalog_index(settings.matcher.index_path, settings.embedder.embedding_dim)
        except Exception as exc:  # noqa: BLE001 — без индекса записываются только фото из папки загрузок
            out(f"Индекс не открывается ({exc}); записываю только фото из папки загрузок.")
    db = (make_session or _open_session)()
    try:
        products = {pid: sku for pid, sku in db.query(Product.id, Product.sku).all()}
        res = backfill(PhotoRegistry(db), products, root, index=index, folder_of=folder_name, list_photos=list_photo_files,
                       upload_ref=upload_ref, apply=args.apply)
    finally:
        db.close()
    verb = "Записано" if args.apply else "Будет записано"
    out(f"{verb} фото из папки загрузок: {res.uploads_registered}; фото импорта (из индекса): {res.import_registered}")
    out(f"{'Перенесено' if args.apply else 'Будет перенесено'} векторов индекса в кэш: {res.vectors_cached}")
    if res.index_model_unknown:
        out("Модель векторов индекса не записана (индекс до 2026-11-14): векторы в кэш не переносятся. Нужна полная пересборка.")
    if res.file_sources_skipped:
        out(f"Векторы полной пересборки по папке (file:…) без записи о фото: {res.file_sources_skipped} — SHA-256 фото неизвестен")
    if res.missing_upload_files:
        out(f"Векторы фото из браузера, файла которых нет: {res.missing_upload_files} (python -m scripts.reconcile_index)")
    for ref, why in res.unreadable[:30]:
        out(f"  НЕ ПРОЧИТАНО {ref}: {why}")
    if not args.apply:
        out("\nЭто только просмотр. Записать: python -m scripts.register_photos --apply")
    return 1 if res.unreadable else 0


if __name__ == "__main__":
    raise SystemExit(main())
