"""
Замер: различает ли поиск по виду размеры одного товара (Pepsi 0,5 л и 1,5 л) — docs/CATALOG_SOURCES.md §5.

Папка — в формате каталога (scripts/import_catalog.py): <артикул>/*.jpg + products.csv (название с размером). Каждое фото по
очереди — «снимок покупателя», остальные фото — каталог («оставить одно»). Для каждого снимка: какой товар ближе всего
(счёт товара = лучшее сходство с его фото), тот ли это размер, и насколько отстаёт другой размер того же товара.

    .venv/Scripts/python.exe -m ml.benchmark_sizes --dir D:/uznat_tovar/catalog-sets/size-check

Модель — та же, что в программе (configs/config.yaml, models.embedder; без дообученных весов — facebook/dinov2-base).
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

import numpy as np

from app.services.catalog_import import IMAGE_EXTS
from app.services.product_size import name_without_size, same_size, size_of


def load_names(folder: Path) -> dict[str, str]:
    with (folder / "products.csv").open(encoding="utf-8-sig", newline="") as f:
        return {r["sku"]: f"{r['name']} {r.get('brand', '')}".strip() for r in csv.DictReader(f, delimiter=";")}


def evaluate(vectors: np.ndarray, owners: list[str], names: dict[str, str]) -> dict:
    """Счёт «оставить одно» -> сводка. Учитываются только снимки товаров, у которых в наборе есть другой размер."""
    groups = {sku: name_without_size(names[sku]) for sku in set(owners)}
    sims = vectors @ vectors.T
    np.fill_diagonal(sims, -1.0)
    skus = sorted(set(owners))
    cols = {s: [i for i, o in enumerate(owners) if o == s] for s in skus}
    out = Counter()
    gaps: list[float] = []
    for i, own in enumerate(owners):
        sizes = [s for s in skus if s != own and groups[s] == groups[own] and not same_size(size_of(names[s]), size_of(names[own]))]
        if not sizes or len(cols[own]) < 2:
            continue
        score = {s: float(max(sims[i, j] for j in cols[s])) for s in skus if cols[s] != [i]}
        best = max(score, key=score.get)
        out["снимков"] += 1
        if best == own:
            out["верный размер первым"] += 1
        elif best in sizes:
            out["другой размер того же товара первым"] += 1
        else:
            out["другой товар первым"] += 1
        gaps.append(score.get(own, -1.0) - max(score[s] for s in sizes))
    g = np.array(gaps) if gaps else np.zeros(1)
    return {**out, "отрыв своего размера от другого: медиана": round(float(np.median(g)), 3),
            "доля снимков, где другой размер ближе 0,15": round(float(np.mean(g < 0.15)), 3),
            "доля снимков, где другой размер ближе 0,05": round(float(np.mean(g < 0.05)), 3)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--device", default="auto")
    a = ap.parse_args()
    from app.core.config import get_settings  # noqa: PLC0415
    from app.pipeline.embedder import create_embedder  # noqa: PLC0415
    from app.pipeline.imageio import load_rgb  # noqa: PLC0415
    folder = Path(a.dir)
    names = load_names(folder)
    cfg = get_settings().embedder
    emb = create_embedder(cfg, device=a.device)
    owners, vecs = [], []
    for sku in sorted(names):
        for p in sorted((folder / sku).glob("*")):
            if p.suffix.lower() in IMAGE_EXTS:
                vecs.append(emb.embed([load_rgb(p)])[0])
                owners.append(sku)
    print(f"Товаров {len(names)}, фото {len(owners)}")
    for k, v in evaluate(np.array(vecs, dtype=np.float32), owners, names).items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
