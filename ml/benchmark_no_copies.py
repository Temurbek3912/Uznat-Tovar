"""
Поиск по виду без «почти копий» (замер 2026-11-18, каталог 500 товаров).

    .venv/Scripts/python.exe -m ml.benchmark_no_copies --dir D:/uznat_tovar/catalog-sets/rich-pilot \
        --dir D:/uznat_tovar/catalog-sets/popular-500 --front-prefix front_ --out data/benchmark/models-500

В открытой базе лицевое фото — часто та же фотография, что и одна из загруженных, только обрезанная. Тогда «оставить одно»
находит копию, а не товар: у 41–48 % фото есть фото того же товара со сходством больше 0,97. Покупатель же снимает свою,
новую фотографию. Здесь из каталога для каждого снимка убираются фото ТОГО ЖЕ товара, которые хотя бы одна из сравниваемых
моделей считает копией (сходство > --copy, по умолчанию 0,93); считаются только снимки, у товара которых осталось другое фото.
Чужие товары в каталоге остаются все. Снимок — как есть и «как покупатель» (ml/benchmark_naming.make_shopper).
Пары моделей сравниваются на одних и тех же снимках (точный тест Макнемара). Пороги — в no_copies_thresholds.csv.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
from pathlib import Path

import numpy as np

from ml.benchmark_models import CANDIDATES, Embedder, mcnemar, scan


def _open(data: bytes):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as im:  # exif-ok
        return ImageOps.exif_transpose(im).convert("RGB")


def rank(sims: np.ndarray, own: np.ndarray, allowed: np.ndarray, only: set[int] | None):
    """Для каждого снимка: (номер, верен ли первый, лучший счёт, лучший чужой). allowed[i, j] — фото j в каталоге для снимка i."""
    out = []
    for i in range(len(own)):
        if only is not None and i not in only:
            continue
        mine_mask = allowed[i] & (own == own[i])
        if not mine_mask.any():
            continue
        mine = sims[i][mine_mask].max()
        other = sims[i][allowed[i] & (own != own[i])].max()
        out.append((i, bool(mine > other), float(max(mine, other)), float(other)))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", action="append", required=True)
    ap.add_argument("--out", default="data/benchmark/models-500")
    ap.add_argument("--models", default="dinov2-base,siglip2-base,dinov2-small")
    ap.add_argument("--front-prefix", default="front_")
    ap.add_argument("--copy", type=float, default=0.93, help="сходство, выше которого фото того же товара — копия")
    a = ap.parse_args(argv)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from ml.benchmark_naming import make_shopper
    items = scan([Path(d) for d in a.dir])
    own = np.array([o for o, _ in items])
    front = {i for i, (_, p) in enumerate(items) if p.name.startswith(a.front_prefix)}
    orig = [_open(p.read_bytes()) for _, p in items]
    shop = [_open(make_shopper(p.read_bytes(), seed=i)) for i, (_, p) in enumerate(items)]
    wanted = [c for c in CANDIDATES if c.key in a.models.split(",")]
    gal, qry = {}, {}
    for cand in wanted:
        emb = Embedder(cand, "cuda")
        gal[cand.key] = np.concatenate([emb.embed(orig[s:s + 32]) for s in range(0, len(orig), 32)])
        qry[cand.key] = np.concatenate([emb.embed(shop[s:s + 32]) for s in range(0, len(shop), 32)])
        del emb
    n = len(items)
    copy = np.zeros((n, n), bool)
    for g in gal.values():
        copy |= (g @ g.T) > a.copy
    same = own[:, None] == own[None, :]
    allowed = ~(same & copy)
    np.fill_diagonal(allowed, False)
    print(f"Фото: {n}, товаров: {len(set(own))}; пар-копий того же товара: {int((same & copy).sum() - n) // 2}", flush=True)
    rows, thr_rows, flags = [], [], {}
    for key in gal:
        for variant, q in (("как есть", gal[key]), ("как покупатель", qry[key])):
            sims = q @ gal[key].T
            for subset, only in (("все снимки", None), ("лицевая", front)):
                res = rank(sims, own, allowed, only)
                flags[(key, variant, subset)] = {i: ok for i, ok, _, _ in res}
                ks = np.array([s for _, _, s, _ in res])
                ok = np.array([o for _, o, _, _ in res])
                unk = np.array([u for _, _, _, u in res])
                thr = float(np.quantile(unk, 0.95))
                rows.append({"модель": key, "снимки": variant, "набор": subset, "снимков": len(res),
                             "top1": round(float(ok.mean()), 3), "порог_5%": round(thr, 3),
                             "уверенно_верно": round(float(np.mean((ks > thr) & ok)), 3),
                             "уверенно_неверно": round(float(np.mean((ks > thr) & ~ok)), 3)})
                print(rows[-1], flush=True)
                for t in np.arange(0.60, 0.981, 0.02):
                    thr_rows.append({"модель": key, "снимки": variant, "набор": subset, "порог": round(float(t), 2),
                                     "чужой_принят": round(float(np.mean(unk >= t)), 3),
                                     "принят_верно": round(float(np.mean((ks >= t) & ok)), 3),
                                     "принят_неверно": round(float(np.mean((ks >= t) & ~ok)), 3),
                                     "известный_ниже": round(float(np.mean(ks < t)), 3)})
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("no_copies.csv", rows), ("no_copies_thresholds.csv", thr_rows)):
        with (out / name).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0]), delimiter=";")
            w.writeheader()
            w.writerows(data)
    base = "dinov2-base"
    for (key, variant, subset), fl in flags.items():
        if key == base:
            continue
        ref = flags[(base, variant, subset)]
        better = sum(1 for i in fl if fl[i] and not ref[i])
        worse = sum(1 for i in fl if ref[i] and not fl[i])
        print(f"  {key} против {base}, {variant}, {subset}: лучше {better}, хуже {worse}, p={mcnemar(better, worse):.4f}")
    print(f"Готово: {out / 'no_copies.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
