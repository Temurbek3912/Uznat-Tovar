"""
Какая «порча» снимка покупателя сбивает модель поиска по виду (замер 2026-11-18, каталог 500 товаров).

    .venv/Scripts/python.exe -m ml.benchmark_distortions --dir D:/uznat_tovar/catalog-sets/rich-pilot \
        --dir D:/uznat_tovar/catalog-sets/popular-500 --front-prefix front_ --out data/benchmark/models-500

ml.benchmark_models портит снимок сразу всем (поворот, мельче на фоне, темнее, размыто, сжатие — ml/benchmark_naming.make_shopper);
здесь — каждое по отдельности и все вместе, на одном общем каталоге (все папки — один каталог, как база программы).
Каталог — всегда фото как есть; снимок — то же фото (оставить одно), испорченное одним способом.
Плюс «почти копии»: доля снимков, у которых фото того же товара в каталоге похоже на него больше 0,97 (одна и та же
фотография, обрезанная иначе, — тогда «как есть» завышает результат).
"""
from __future__ import annotations

import argparse
import csv
import io
import random
from pathlib import Path

import numpy as np

from ml.benchmark_models import CANDIDATES, Embedder, evaluate, scan


def _open(data: bytes):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as im:  # exif-ok
        return ImageOps.exif_transpose(im).convert("RGB")


def distort(img, kind: str, seed: int):
    """Одна порча из make_shopper с теми же диапазонами (ml/benchmark_naming.py)."""
    from PIL import Image, ImageEnhance, ImageFilter
    rnd = random.Random(seed)
    im = img.copy()
    im.thumbnail((900, 900))
    if kind == "поворот":
        return im.rotate(rnd.uniform(-12, 12), expand=True, fillcolor=(120, 115, 105))
    if kind == "мельче на фоне":
        scale = rnd.uniform(0.55, 0.75)
        w, h = im.size
        canvas = Image.new("RGB", (int(w / scale), int(h / scale)), tuple(rnd.randint(60, 170) for _ in range(3)))
        canvas.paste(im, (rnd.randint(0, canvas.width - w), rnd.randint(0, canvas.height - h)))
        return canvas
    if kind == "темнее":
        return ImageEnhance.Brightness(im).enhance(rnd.uniform(0.6, 0.85))
    if kind == "размыто":
        return im.filter(ImageFilter.GaussianBlur(rnd.uniform(1.0, 2.2)))
    if kind == "размыто слабо":
        return im.filter(ImageFilter.GaussianBlur(rnd.uniform(0.5, 1.0)))
    if kind == "сжатие":
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=55)
        return _open(out.getvalue())
    raise ValueError(kind)


def score_lists(g: np.ndarray, q: np.ndarray, owners: list[str], only=None):
    """Для подбора порогов: (лучший счёт, верен ли первый) у известных товаров и лучший чужой счёт («товара нет в каталоге»)."""
    own = np.array(owners)
    sims = q @ g.T
    known, unknown = [], []
    for i in range(len(owners)):
        if only is not None and i not in only:
            continue
        mask = np.ones(len(owners), bool)
        mask[i] = False
        if not (mask & (own == own[i])).any():
            continue
        s = sims[i].copy()
        s[~mask] = -2
        mine = s[own == own[i]].max()
        other = s[own != own[i]].max()
        known.append((max(mine, other), bool(mine >= other)))
        unknown.append(other)
    return known, np.array(unknown)


def thresholds_table(known, unknown, grid) -> list[dict]:
    """По каждому порогу: сколько «товара нет» ошибочно принято, сколько известных принято верно / неверно, сколько ниже."""
    ks = np.array([k for k, _ in known])
    ok = np.array([c for _, c in known])
    return [{"порог": round(float(t), 3), "чужой_принят": round(float(np.mean(unknown >= t)), 3),
             "принят_верно": round(float(np.mean((ks >= t) & ok)), 3), "принят_неверно": round(float(np.mean((ks >= t) & ~ok)), 3),
             "известный_ниже": round(float(np.mean(ks < t)), 3)} for t in grid]


KINDS = ("поворот", "мельче на фоне", "темнее", "размыто", "размыто слабо", "сжатие")


def main(argv=None) -> int:
    import os
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", action="append", required=True)
    ap.add_argument("--out", default="data/benchmark/models-500")
    ap.add_argument("--models", default="dinov2-base,siglip2-base")
    ap.add_argument("--front-prefix", default="front_")
    a = ap.parse_args(argv)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from ml.benchmark_naming import make_shopper
    items = scan([Path(d) for d in a.dir])
    owners = [o for o, _ in items]
    groups = ["каталог"] * len(items)
    front = {i for i, (_, p) in enumerate(items) if p.name.startswith(a.front_prefix)}
    orig = [_open(p.read_bytes()) for _, p in items]
    variants = {k: [distort(im, k, i) for i, im in enumerate(orig)] for k in KINDS}
    variants["всё вместе (как покупатель)"] = [_open(make_shopper(p.read_bytes(), seed=i)) for i, (_, p) in enumerate(items)]
    print(f"Фото: {len(items)}, товаров: {len(set(owners))}, лицевых снимков: {len(front)}", flush=True)
    rows, thr_rows = [], []
    own = np.array(owners)
    for cand in [c for c in CANDIDATES if c.key in a.models.split(",")]:
        emb = Embedder(cand, "cuda")

        def vecs(images):
            return np.concatenate([emb.embed(images[s:s + 32]) for s in range(0, len(images), 32)])

        g = vecs(orig)
        sims = g @ g.T
        np.fill_diagonal(sims, -2)
        same = own[:, None] == own[None, :]
        best_own = np.where(same, sims, -2).max(axis=1)
        dup = float(np.mean(best_own[best_own > -2] > 0.97))
        print(f"{cand.key}: почти копий (своё фото > 0,97): {dup:.1%}", flush=True)
        for name, images in [("как есть", None), *variants.items()]:
            q = g if images is None else vecs(images)
            if name in ("как есть", "всё вместе (как покупатель)"):
                for subset, only in (("все снимки", None), ("лицевая", front)):
                    known, unknown = score_lists(g, q, owners, only)
                    for r in thresholds_table(known, unknown, np.arange(0.60, 0.98, 0.02)):
                        thr_rows.append({"модель": cand.key, "порча": name, "снимки": subset, **r})
            for subset, only in (("все снимки", None), ("лицевая", front)):
                res = evaluate(g, q, owners, groups, only)
                rows.append({"модель": cand.key, "порча": name, "снимки": subset, "снимков": res["снимков"],
                             "top1": round(res["top1"], 3), "top3": round(res["top3"], 3), "порог_5%": round(res["порог"], 3),
                             "уверенно_верно": round(res["уверенно_верно"], 3), "почти_копий": round(dup, 3)})
                print(rows[-1], flush=True)
        del emb
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "distortions.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)
    with (out / "thresholds.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(thr_rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(thr_rows)
    print(f"Готово: {out / 'distortions.csv'}, {out / 'thresholds.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
