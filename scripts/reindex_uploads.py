"""
Доиндексировать фото товаров, добавленных из браузера («Товары» → «Добавить товар» / «＋ Фото»), которых нет в индексе.

Что «есть в индексе», решает САМ индекс: у каждого вектора записано, из какого фото он получен (2026-11-07,
app/services/index_coverage.py). Поэтому скрипт находит и точечно восстанавливает:
  - фото товара, который записан в базу, но векторы которого не успели сохраниться (сбой, потерянная задача очереди);
  - часть фото товара, векторы которых потеряны;
  - фото, добавленные «＋ Фото», когда очередь была недоступна.
Повторный запуск безопасен: уже проиндексированные фото пропускаются, дублей нет; обрыв не портит индекс. Воркер делает то же
самое автоматически при каждом запуске (задача recover_index), так что скрипт нужен, когда воркер остановлен или для проверки.
Журнал uploads_manifest.json учитывается только для товаров с векторами версий до 2026-11-07 (у них нет записи об источнике).
С --apply добавленные фото записываются и в базу (product_images, кэш векторов image_embeddings) — до записи в индекс.

    python -m scripts.reindex_uploads            # только показать, что будет добавлено (ничего не меняет, модель не грузится)
    python -m scripts.reindex_uploads --apply    # добавить

Порядок полной пересборки: python -m ml.build_catalog_embeddings … --replace-existing (или import_catalog), затем этот скрипт.
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


def _load_embedder(settings):
    from app.pipeline.embedder import create_embedder  # noqa: PLC0415
    return create_embedder(settings.embedder)


def _open_registry():
    from app.db.session import SessionLocal                       # noqa: PLC0415
    from app.services.product_images import PhotoRegistry         # noqa: PLC0415
    return PhotoRegistry(SessionLocal())


def main(argv: list[str] | None = None, out=print, load_products=None, settings=None, resolve_path=None, make_embedder=None,
         load_excluded=None,
         load_image=None, make_registry=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="действительно добавить фото в индекс (без флага — только показать)")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    args = ap.parse_args(argv)

    from app.pipeline.index_store import IndexBusyError                                  # noqa: PLC0415
    from app.pipeline.matcher import open_catalog_index                                  # noqa: PLC0415
    from app.services import index_coverage as C                                         # noqa: PLC0415
    from app.services.catalog_upload import (CatalogAddError, UploadsManifest, folder_name, list_photo_files,  # noqa: PLC0415
                                             manifest_path_for, upload_ref, uploads_root)
    if settings is None or resolve_path is None:
        from app.core.config import get_settings, resolve_project_path                  # noqa: PLC0415
        settings = settings or get_settings()
        resolve_path = resolve_path or resolve_project_path
    if settings.matcher is None or settings.embedder is None:
        out("В configs/config.yaml не заданы models.embedder / models.matcher")
        return 2
    index_path = settings.matcher.index_path
    root = uploads_root(resolve_path(settings.media_root))
    manifest_path = manifest_path_for(index_path)
    try:
        manifest = UploadsManifest.load(manifest_path)
        index = open_catalog_index(index_path, settings.embedder.embedding_dim)
    except (CatalogAddError, Exception) as exc:  # noqa: BLE001 — причина человеку; индекс не трогаем
        out(f"Не удалось открыть индекс или журнал: {exc}")
        return 2
    products = (load_products or _load_products)()
    excluded = (load_excluded or (_load_excluded if load_products is None else frozenset))()
    rep = C.analyze_coverage(index, products, root, folder_of=folder_name, list_photos=list_photo_files, upload_ref=upload_ref,
                             manifest_keys=manifest.keys, skip=excluded)
    if not rep.photos_not_indexed:
        out(f"Нечего добавлять: все фото из {root} уже в индексе.")
        return 0
    by_sku: dict[str, int] = {}
    for _pid, sku, _ref in rep.photos_not_indexed:
        by_sku[sku] = by_sku.get(sku, 0) + 1
    out(f"Не в индексе: {len(rep.photos_not_indexed)} фото у {len(by_sku)} товаров:")
    for sku, n in list(by_sku.items())[:30]:
        out(f"  {sku:<30} — {n} фото")
    if not args.apply:
        out("\nЭто только просмотр. Чтобы добавить в индекс: python -m scripts.reindex_uploads --apply")
        return 0
    if not args.yes and input("Добавить в индекс? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
        out("Отменено.")
        return 1

    if load_image is None:
        from app.pipeline.imageio import load_rgb as load_image                         # noqa: PLC0415
    out("Загружаю модель эмбеддера (при первом запуске скачает веса из интернета)...")
    embedder = (make_embedder or _load_embedder)(settings)
    from app.pipeline.embedder import model_signature                                    # noqa: PLC0415
    from app.pipeline.matcher import check_index_model                                   # noqa: PLC0415
    try:
        check_index_model(index, index_path, model_signature(embedder))
    except Exception as exc:  # noqa: BLE001 — индекс другой модели: добавлять в него векторы нельзя
        out(f"Индекс нельзя дополнить этой моделью: {exc}")
        return 2

    def note(refs):
        # журнал — удобство: векторы уже в индексе и с записью об источнике; сбой журнала не останавливает доиндексацию
        manifest.add(refs)
        try:
            manifest.save()
        except (CatalogAddError, IndexBusyError, OSError) as exc:
            out(f"  Журнал проиндексированных фото не записан ({exc}) — на работу поиска это не влияет")

    # записи о фото в базе: при настоящем запуске — всегда; тесты, подающие товары сами, подают и реестр (или обходятся без него)
    registry = (make_registry or (_open_registry if load_products is None else lambda: None))()
    remember = None
    if registry is not None:
        from app.services.product_images import upload_photo_info                       # noqa: PLC0415
        signature = model_signature(embedder)

        def remember(pid, refs, vectors):
            registry.remember(pid, [upload_photo_info(root, r) for r in refs], vectors, signature)

    try:
        res = C.recover_pending(index, products, root, embedder=embedder, index_path=index_path, folder_of=folder_name,
                                list_photos=list_photo_files, upload_ref=upload_ref, load_image=load_image,
                                manifest_keys=manifest.keys, note_indexed=note, limit=10 ** 9, remember=remember,
                                skip=excluded)
    finally:
        if registry is not None:
            registry.db.close()
    for pid, ref, why in res.failed:
        out(f"  СБОЙ {ref} (товар {pid}): {why}")
    out(f"\nГотово: добавлено фото {len(res.added)}, не прочитано {len(res.failed)}. Индекс: {index_path}")
    return 1 if res.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
