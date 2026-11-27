"""
Датасет фото товаров магазина для дообучения поиска по виду (ml/finetune_embedder.py, Kaggle или ноутбук).

    set LITE_MODE=1 & set DATABASE_URL=sqlite:///D:/uznat_tovar/retail-shelf-audit/data/lite/shop.db
    set MEDIA_ROOT=D:/uznat_tovar/retail-shelf-audit/data/lite
    set CATALOG_INDEX_PATH=D:/uznat_tovar/retail-shelf-audit/data/lite/index/catalog.faiss
    .venv/Scripts/python.exe -m ml.shop_dataset --out D:/uznat_tovar/kaggle-embedder/dataset

Берёт ровно те фото, по которым меряет ml/train_projection.py --check-only: активные эталоны активных товаров, у которых
есть вектор модели рабочего индекса в кэше базы. Только ЧИТАЕТ базу и фото. Пишет в --out:
  images/<id>.jpg   — фото, уменьшенные до --side точек по длинной стороне (JPEG), чтобы загрузка шла быстрее;
  photos.csv        — image (номер строки = номер вектора), product, front (лицевая), part (часть товаров 0–4 для честной
                      проверки — те же части, что в ml/train_projection.py);
  base_vectors.npy  — векторы этих фото из кэша базы (SigLIP 2 без проекции, по исходным фото) — для сверки и замера;
и архив <out>.zip рядом — фото, photos.csv и ml/finetune_embedder.py (его загружают на Kaggle как Dataset).
"""
from __future__ import annotations

import argparse
import csv
import io
import shutil
import sys
from pathlib import Path

import numpy as np


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--side", type=int, default=288, help="длинная сторона фото в датасете (модель видит 224×224)")
    ap.add_argument("--quality", type=int, default=85)
    a = ap.parse_args(argv)
    from PIL import Image  # noqa: PLC0415

    from app.core.config import get_settings, resolve_project_path  # noqa: PLC0415
    from app.db.session import SessionLocal  # noqa: PLC0415
    from app.services import product_images as P  # noqa: PLC0415
    from ml.train_projection import load_catalog_vectors, product_parts  # noqa: PLC0415

    settings = get_settings()
    vectors, owners, front, signature, missing = load_catalog_vectors(settings)
    # тот же порядок, что у load_catalog_vectors: план пересборки, только фото с вектором в кэше
    from app.db.models import Product  # noqa: PLC0415
    from app.pipeline.embedder import model_version  # noqa: PLC0415
    db = SessionLocal()
    try:
        products = {pid for pid, status in db.query(Product.id, Product.status).all() if status != "archived"}
        plan = P.plan_rebuild(P.PhotoRegistry(db), products, model_version(signature))
    finally:
        db.close()
    items = plan.cached
    assert [i.product_id for i in items] == list(owners)
    media = Path(resolve_project_path(settings.media_root))
    parts = product_parts(owners)
    part_of = {p: k for k, ps in enumerate(parts) for p in ps}

    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    (out / "images").mkdir(parents=True)
    rows, total = [], 0
    for n, item in enumerate(items):
        src = media / item.storage_path
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((a.side, a.side), Image.BICUBIC)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=a.quality, optimize=True)
        name = f"{n:05d}.jpg"
        (out / "images" / name).write_bytes(buf.getvalue())
        total += buf.tell()
        rows.append({"image": name, "product": item.product_id, "front": int(bool(front[n])), "part": part_of[item.product_id]})
    with open(out / "photos.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["image", "product", "front", "part"])
        w.writeheader()
        w.writerows(rows)
    np.save(out / "base_vectors.npy", vectors.astype(np.float32))
    import zipfile  # noqa: PLC0415
    archive = str(out) + ".zip"                      # на Kaggle нужны только фото и photos.csv (векторы — для замера дома)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:   # JPEG не сжимается дальше
        zf.write(out / "photos.csv", "photos.csv")
        zf.write(Path(__file__).with_name("finetune_embedder.py"), "finetune_embedder.py")   # скрипт обучения — для Kaggle
        for row in rows:
            zf.write(out / "images" / row["image"], "images/" + row["image"])
    size = Path(archive).stat().st_size
    print(f"Модель индекса: {signature.get('name')}; фото: {len(rows)}, товаров: {len(set(owners))}"
          + (f"; без вектора в кэше (не вошли): {missing}" if missing else ""))
    print(f"Фото в датасете: {total / 1e6:.1f} МБ; архив {archive}: {size / 1e6:.1f} МБ "
          f"(загрузка при 80 КБ/с — около {size / 80e3 / 60:.0f} мин)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
