"""
Пересборка индекса визуального поиска из базы: активные эталонные фото (product_images) и их векторы из кэша
(image_embeddings). Модель распознавания для этого НЕ загружается, если у всех фото есть вектор нужной версии модели.

    python -m scripts.rebuild_index                       # только показать план (ничего не меняет)
    python -m scripts.rebuild_index --apply               # пересобрать рабочий индекс (сначала копия прежнего рядом)
    python -m scripts.rebuild_index --apply --compute-missing   # фото без вектора в кэше посчитать моделью (загрузит её)

Версия модели: --model-version, иначе — модель текущего индекса (если записана), иначе — единственная модель в кэше.
Безопасность:
  - прежние файлы индекса и журнала копируются в models/index/backup-<время>/ (копии сверяются по SHA-256) и не удаляются;
  - если в новый индекс не попадёт вектор, который есть в текущем (фото без вектора в кэше, старые векторы без записи о фото),
    — отказ без изменений; согласиться на это можно только явно: --allow-missing;
  - после записи индекс перечитывается и сверяется с ожидаемым.
Фото и записи о них не меняются. Воркер можно не останавливать: он перечитает изменённый индекс сам.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def _open_session():
    from app.db.session import SessionLocal     # noqa: PLC0415 — ленивые импорты: тесты подают сессию сами
    return SessionLocal()


def _load_embedder(settings):
    from app.pipeline.embedder import create_embedder  # noqa: PLC0415
    return create_embedder(settings.embedder)


def _lost_vectors(current, products: set[str], kept: set[tuple[str, str]]) -> int:
    """Сколько векторов текущего индекса (у товаров из базы) не попадёт в новый."""
    if current is None:
        return 0
    sources = getattr(current, "_sources", None) or [None] * len(current._ids)
    return sum(1 for pid, src in zip(current._ids, sources, strict=True) if pid in products and (pid, src) not in kept)


def main(argv: list[str] | None = None, out=print, settings=None, resolve_path=None, make_session=None, make_embedder=None,
         load_image=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="пересобрать (без флага — только показать план)")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    ap.add_argument("--model-version", help="версия модели из кэша (по умолчанию — модель текущего индекса)")
    ap.add_argument("--compute-missing", action="store_true", help="векторы фото без кэша посчитать моделью (загрузит её)")
    ap.add_argument("--allow-missing", action="store_true", help="разрешить новый индекс без части векторов текущего")
    ap.add_argument("--output-index", help="записать новый индекс в другой путь (рабочий не трогается)")
    args = ap.parse_args(argv)

    from app.db.models import Product                                               # noqa: PLC0415
    from app.pipeline.embedder import model_signature, model_version                # noqa: PLC0415
    from app.pipeline.matcher import open_catalog_index                             # noqa: PLC0415
    from app.services import index_reconcile as R                                   # noqa: PLC0415
    from app.services import product_images as P                                    # noqa: PLC0415
    from app.services.catalog_upload import (folder_name, list_photo_files, manifest_path_for, upload_ref,  # noqa: PLC0415
                                             uploads_root)
    if settings is None or resolve_path is None:
        from app.core.config import get_settings, resolve_project_path             # noqa: PLC0415
        settings = settings or get_settings()
        resolve_path = resolve_path or resolve_project_path
    if settings.matcher is None or settings.embedder is None:
        out("В configs/config.yaml не заданы models.embedder / models.matcher")
        return 2
    index_path = settings.matcher.index_path
    output = args.output_index or index_path
    dim = settings.embedder.embedding_dim
    current = None
    try:
        current = open_catalog_index(index_path, dim)
    except Exception as exc:  # noqa: BLE001 — повреждённый индекс как раз и пересобирают
        out(f"Текущий индекс не открывается ({exc}) — строю новый из базы.")
    embedder = None
    db = (make_session or _open_session)()
    try:
        registry = P.PhotoRegistry(db)
        version = args.model_version
        if args.compute_missing:
            out("Загружаю модель эмбеддера...")
            embedder = (make_embedder or _load_embedder)(settings)
            loaded = model_version(model_signature(embedder))
            if loaded is None or (version and version != loaded):
                out("Загруженная модель не та, что указана в --model-version (или не сообщает паспорт) — отказ.")
                return 2
            version = loaded
            registry.ensure_model(model_signature(embedder))
        if version is None and current is not None and getattr(current, "model", None) is not None:
            version = model_version(current.model)
        if version is None:
            versions = registry.model_versions()
            if len(versions) != 1:
                out("Не понятно, для какой модели строить индекс: модель текущего индекса не записана, а моделей в кэше "
                    f"{len(versions)}. Укажите --model-version ({', '.join(versions) or 'кэш пуст'}) или --compute-missing.")
                return 2
            version = versions[0]
        signature = registry.signature_of(version)
        if signature is None:
            out(f"Модели {version} нет в базе — отказ.")
            return 2
        product_skus = {pid: sku for pid, sku in db.query(Product.id, Product.sku).all()}
        products = set(product_skus)
        # сначала записи о фото, которых в базе ещё нет (папка загрузок, импорт из индекса), и векторы индекса известной
        # модели — в кэш (как register_photos); при просмотре — только подсчёт
        bf = P.backfill(registry, product_skus, uploads_root(resolve_path(settings.media_root)), index=current,
                        folder_of=folder_name, list_photos=list_photo_files, upload_ref=upload_ref, apply=args.apply)
        if not args.apply and (bf.uploads_registered or bf.import_registered or bf.vectors_cached):
            out(f"Ещё не записаны в базу: фото {bf.uploads_registered + bf.import_registered}, векторов индекса в кэш "
                f"{bf.vectors_cached} — запишутся при --apply (в план ниже не входят).")
        plan = P.plan_rebuild(registry, products, version)
        kept = {(i.product_id, i.source) for i in plan.items if i.vector is not None or embedder is not None}
        lost = _lost_vectors(current, products, kept) if output == index_path else 0
        out(f"Модель: {signature.get('name')} ({version})")
        out(f"Активных эталонов: {len(plan.items)}; вектор в кэше есть у {len(plan.cached)}, нет у {len(plan.missing)}; "
            f"выведено из эталонов: {plan.inactive}")
        if current is not None:
            out(f"В текущем индексе векторов: {len(current._ids)}; не войдут в новый: {lost}")
        if plan.missing and embedder is None:
            out("Фото без вектора в кэше войдут в индекс только с --compute-missing (нужна модель).")
        if not args.apply:
            out("\nЭто только просмотр. Пересобрать: python -m scripts.rebuild_index --apply")
            return 0
        if (lost or (plan.missing and embedder is None)) and not args.allow_missing:
            out("Отказ: в новом индексе не хватило бы векторов (см. выше). Сначала: python -m scripts.register_photos --apply "
                "и/или --compute-missing; согласиться на потерю — --allow-missing.")
            return 1
        if not args.yes and input("Пересобрать индекс? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
            out("Отменено.")
            return 1
        result = P.build_index(plan, signature, dim, registry=registry, embedder=embedder,
                               media_root=Path(resolve_path(settings.media_root)), load_image=load_image,
                               allow_missing=args.allow_missing)
    finally:
        db.close()
    if R._index_files(output):
        backup = R.backup_files(output, [manifest_path_for(output)])
        out(f"Копия прежнего индекса: {backup}")
    result.index.save(output)
    check = open_catalog_index(output, dim, signature)
    if check._ids != result.index._ids or list(check._sources) != list(result.index._sources):
        out(f"ПРОВЕРКА НЕ ПРОШЛА: записанный индекс {output} не совпал с ожидаемым. Прежние файлы — в копии.")
        return 2
    out(f"Готово: векторов {len(check._ids)} (из кэша {result.from_cache}, посчитано {result.computed}). Индекс: {output}")
    for pid, source, why in result.left_out[:30]:
        out(f"  НЕ ВОШЛО {source} (товар {pid}): {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
