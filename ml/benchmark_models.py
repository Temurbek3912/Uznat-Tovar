"""
Сравнение моделей поиска товара ПО ВИДУ на фото каталога владельца (вопрос «есть ли модели лучше, быстрее, легче», 2026-11-17).

    .venv/Scripts/python.exe -m ml.benchmark_models --dir D:/uznat_tovar/catalog-sets/rich-pilot \
        --dir D:/uznat_tovar/catalog-sets/size-check --out data/benchmark/models

Папки — в формате каталога (scripts/import_catalog.py): <артикул>/*.jpg (+ products.csv; товары категории «Алкоголь» пропускаются —
их нет в базе). Замер «оставить одно», как самопроверка каталога: каждое фото по очереди — снимок покупателя, остальные фото —
каталог; счёт товара — лучшее сходство с его фото (как в боевом поиске). Каждый снимок проверяется в двух видах: как есть и
«как снимает покупатель» (поворот, мельче, темнее, размыто — ml/benchmark_naming.make_shopper); каталог — всегда как есть.

Что считается для каждой модели:
* первым найден верный товар (top-1) и верный в первой тройке (top-3);
* «уверенно и верно»: порог выбирается так, чтобы товар НЕ из каталога (все его фото убраны из каталога) принимался не чаще
  5 % — и считается, сколько своих снимков выше порога и найдены верно (порог у каждой модели свой, сравнение честное);
* время на один снимок (видеокарта и процессор, по одному кадру, как в программе), память видеокарты, размер весов на диске.
Ошибки, общие для всех моделей, записываются отдельно: это кандидаты в «плохие фото», а не в «плохая модель».
С --labels (ручная разметка «лицевая / не лицевая») добавляется набор «только лицевые фото» — и снимок, и каталог.

Скачивание весов — только с --allow-download (в кэш Hugging Face, HF_HOME). Фото и результаты — в data/ (не в git).
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SKIP_CATEGORIES = ("алкоголь", "табак")
FALSE_ACCEPT = 0.05


@dataclass(frozen=True)
class Candidate:
    key: str
    repo: str
    kind: str              # dinov2 | siglip | clip
    pad: bool = False      # дополнить кадр до квадрата (иначе процессор DINOv2 обрезает края высоких бутылок)
    license: str = ""


CANDIDATES = (
    Candidate("dinov2-small", "facebook/dinov2-small", "dinov2", license="Apache-2.0"),
    Candidate("dinov2-base", "facebook/dinov2-base", "dinov2", license="Apache-2.0"),
    Candidate("dinov2-base-pad", "facebook/dinov2-base", "dinov2", pad=True, license="Apache-2.0"),
    Candidate("dinov2-large", "facebook/dinov2-large", "dinov2", license="Apache-2.0"),
    Candidate("siglip2-base", "google/siglip2-base-patch16-224", "siglip", license="Apache-2.0"),
    Candidate("siglip2-base-384", "google/siglip2-base-patch16-384", "siglip", license="Apache-2.0"),
    Candidate("clip-b32", "openai/clip-vit-base-patch32", "clip", license="MIT"),
)


def pad_square(img):
    from PIL import Image
    w, h = img.size
    side = max(w, h)
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    return canvas


class Embedder:
    def __init__(self, cand: Candidate, device: str):
        import torch
        from transformers import AutoImageProcessor, AutoModel
        self.torch, self.cand, self.device = torch, cand, device
        self.processor = AutoImageProcessor.from_pretrained(cand.repo)
        if cand.kind == "siglip":
            from transformers import SiglipVisionModel
            model = SiglipVisionModel.from_pretrained(cand.repo)
        elif cand.kind == "clip":
            from transformers import CLIPVisionModelWithProjection
            model = CLIPVisionModelWithProjection.from_pretrained(cand.repo)
        else:
            model = AutoModel.from_pretrained(cand.repo)
        self.params = sum(p.numel() for p in model.parameters())
        self.model = model.to(device).eval()

    def to(self, device: str) -> None:
        self.device = device
        self.model = self.model.to(device)

    def embed(self, images) -> np.ndarray:
        if self.cand.pad:
            images = [pad_square(im) for im in images]
        inputs = self.processor(images=images, return_tensors="pt").to(self.device)
        with self.torch.no_grad():
            out = self.model(**inputs)
            if self.cand.kind == "siglip":
                vec = out.pooler_output
            elif self.cand.kind == "clip":
                vec = out.image_embeds
            else:
                vec = out.last_hidden_state[:, 0, :]          # CLS-токен — как DinoV2Embedder программы
        vec = vec.float().cpu().numpy()
        return vec / np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12)


def scan(dirs: list[Path]) -> list[tuple[str, Path]]:
    items = []
    for d in dirs:
        skip = set()
        csv_path = d / "products.csv"
        if csv_path.is_file():
            with csv_path.open(encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f, delimiter=";"):
                    if any(w in (r.get("category") or "").lower() for w in SKIP_CATEGORIES):
                        skip.add(r["sku"])
        for sku_dir in sorted(p for p in d.iterdir() if p.is_dir() and p.name not in skip):
            for p in sorted(sku_dir.iterdir()):
                if p.suffix.lower() in IMAGE_EXTS:
                    items.append((f"{d.name}/{sku_dir.name}", p))
    return items


def load_images(items, shopper: bool):
    from PIL import Image, ImageOps
    from ml.benchmark_naming import make_shopper
    out = []
    for i, (_, p) in enumerate(items):
        data = p.read_bytes()
        if shopper:
            data = make_shopper(data, seed=i)
        with Image.open(io.BytesIO(data)) as im:  # exif-ok
            out.append(ImageOps.exif_transpose(im).convert("RGB"))
    return out


def evaluate(gal: np.ndarray, qry: np.ndarray, owners: list[str], groups: list[str], only=None) -> dict:
    """Оставить одно: снимок i против всех ДРУГИХ фото. Только товары, у которых есть хотя бы 2 фото.
    groups — набор (папка): снимок ищется только среди товаров своего набора (как в отдельном магазине).
    only — номера фото, которые берутся снимками (например, только лицевые); каталог — все фото."""
    sims = qry @ gal.T
    n = len(owners)
    own = np.array(owners)
    grp = np.array(groups)
    top1 = top3 = total = 0
    known_ok_scores, known_wrong_scores, unknown_scores, wrong_idx, counted = [], [], [], [], []
    for i in range(n):
        mask = (grp == grp[i])
        mask[i] = False
        if only is not None and i not in only:
            continue
        if not (mask & (own == own[i])).any():
            continue
        scores: dict[str, float] = {}
        for j in np.flatnonzero(mask):
            s = float(sims[i, j])
            if s > scores.get(own[j], -2.0):
                scores[own[j]] = s
        ranked = sorted(scores, key=scores.get, reverse=True)
        total += 1
        counted.append(i)
        if ranked[0] == own[i]:
            top1 += 1
            known_ok_scores.append(scores[ranked[0]])
        else:
            known_wrong_scores.append(scores[ranked[0]])
            wrong_idx.append(i)
        top3 += own[i] in ranked[:3]
        unknown_scores.append(max(v for k, v in scores.items() if k != own[i]))   # товара нет в каталоге: лучший чужой
    thr = float(np.quantile(unknown_scores, 1 - FALSE_ACCEPT))
    sure_ok = sum(s > thr for s in known_ok_scores)
    sure_wrong = sum(s > thr for s in known_wrong_scores)
    return {"снимков": total, "top1": top1 / total, "top3": top3 / total, "порог": thr,
            "уверенно_верно": sure_ok / total, "уверенно_неверно": sure_wrong / total,
            "чужой_выше_0.72": float(np.mean(np.array(unknown_scores) >= 0.72)),      # порог программы (config.yaml) — для DINOv2
            "wrong": wrong_idx, "counted": counted}


def mcnemar(b: int, c: int) -> float:
    """Точный двусторонний тест Макнемара: вероятность такой или большей разницы, если модели на самом деле равны."""
    from math import comb
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def disk_mb(repo: str) -> float:
    from huggingface_hub import scan_cache_dir
    for r in scan_cache_dir().repos:
        if r.repo_id == repo:
            return sum(f.size_on_disk for rev in r.revisions for f in rev.files if f.file_name.endswith((".safetensors", ".bin"))
                       and "flax" not in f.file_name and "tf_" not in f.file_name) / 2**20 / max(len(r.revisions), 1)
    return float("nan")


def timing(emb: Embedder, images, device: str, n: int = 20) -> float:
    emb.to(device)
    emb.embed(images[:1])
    if device == "cuda":
        emb.torch.cuda.synchronize()
    t = time.perf_counter()
    for im in images[:n]:
        emb.embed([im])
    if device == "cuda":
        emb.torch.cuda.synchronize()
    return (time.perf_counter() - t) * 1000 / n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", action="append", required=True)
    ap.add_argument("--out", default="data/benchmark/models")
    ap.add_argument("--models", default="")
    ap.add_argument("--allow-download", action="store_true")
    ap.add_argument("--labels", default="", help="разметка фото (photo_labels.csv): вид «лицевая» — отдельный набор")
    ap.add_argument("--one-catalog", action="store_true",
                    help="все папки — один каталог (как база программы), а не отдельные магазины")
    ap.add_argument("--front-prefix", default="",
                    help="фото с таким началом имени (front_) — лицевые: добавить вид «снимок — лицевая» (каталог — все фото)")
    a = ap.parse_args(argv)
    if not a.allow_download:
        os.environ["HF_HUB_OFFLINE"] = "1"
    import torch
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    items = scan([Path(d) for d in a.dir])
    if a.labels:                         # ручная разметка: добавить набор «только лицевые фото» (и снимок, и каталог)
        with open(a.labels, encoding="utf-8-sig", newline="") as f:
            front = {r["фото"] for r in csv.DictReader(f, delimiter=";") if r["вид"] == "лицевая"}
        items += [(f"{o.split('/')[0]}-лицевые/{o.split('/', 1)[1]}", p) for o, p in items if str(p) in front]
    owners = [o for o, _ in items]
    groups = [o.split("/")[0] for o in owners]
    if a.one_catalog:
        groups = ["каталог-лицевые" if "-лицевые" in gr else "каталог" for gr in groups]
    front_idx = {i for i, (_, p) in enumerate(items) if a.front_prefix and p.name.startswith(a.front_prefix)}
    if a.labels:   # размеченные лицевые фото пилота — тоже лицевые снимки
        front_idx |= {i for i, (o, p) in enumerate(items) if "-лицевые" not in o and str(p) in front}
    print(f"Фото: {len(items)}, товаров: {len(set(owners))}", flush=True)
    orig = load_images(items, shopper=False)
    shop = load_images(items, shopper=True)
    wanted = [c for c in CANDIDATES if not a.models or c.key in a.models.split(",")]
    sets = sorted(set(groups))
    rows, flags = [], {}                     # flags[(модель, набор, вид снимков)] = {номер фото: верно ли}
    for cand in wanted:
        try:
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
            emb = Embedder(cand, "cuda")
        except Exception as exc:  # noqa: BLE001 — нет весов: пропуск с причиной
            print(f"{cand.key}: пропуск ({exc.__class__.__name__}: {str(exc)[:120]})", flush=True)
            continue
        g = np.concatenate([emb.embed(orig[s:s + 16]) for s in range(0, len(orig), 16)])
        q = np.concatenate([emb.embed(shop[s:s + 16]) for s in range(0, len(shop), 16)])
        vram = torch.cuda.max_memory_allocated() / 2**20
        gpu_ms = timing(emb, orig, "cuda")
        cpu_ms = timing(emb, orig, "cpu", n=10)
        for sub in sets:
            idx = [i for i, gr in enumerate(groups) if gr == sub]
            variants = [("как есть", g, None), ("как покупатель", q, None)]
            fr = {j for j, i in enumerate(idx) if i in front_idx}
            if fr:
                variants += [("лицевая, как есть", g, fr), ("лицевая, как покупатель", q, fr)]
            for variant, qv, only in variants:
                res = evaluate(g[idx], qv[idx], [owners[i] for i in idx], [groups[i] for i in idx], only)
                wrong = {idx[j] for j in res.pop("wrong")}
                counted = {idx[j] for j in res.pop("counted")}
                flags[(cand.key, sub, variant)] = {i: i not in wrong for i in counted}
                rows.append({"модель": cand.key, "набор": sub, "снимки": variant,
                             **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in res.items()},
                             "мс_видеокарта": round(gpu_ms, 1), "мс_процессор": round(cpu_ms, 1),
                             "видеопамять_МБ": round(vram), "параметров_млн": round(emb.params / 1e6, 1),
                             "диск_МБ": round(disk_mb(cand.repo)), "лицензия": cand.license})
                print(rows[-1], flush=True)
        del emb
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        keys = list(dict.fromkeys(k for r in rows for k in r))
        w = csv.DictWriter(f, fieldnames=keys, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    # парное сравнение с текущей моделью: на скольких снимках одна права, а другая нет (точный тест Макнемара)
    base = "dinov2-base"
    with (out / "paired.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["модель", "набор", "снимки", "лучше текущей", "хуже текущей", "p (Макнемар)"])
        for (key, sub, variant), fl in flags.items():
            if key == base or (base, sub, variant) not in flags:
                continue
            ref = flags[(base, sub, variant)]
            better = sum(1 for i in fl if fl[i] and not ref[i])
            worse = sum(1 for i in fl if ref[i] and not fl[i])
            w.writerow([key, sub, variant, better, worse, round(mcnemar(better, worse), 4)])
            print(f"  {key} против {base}, {sub}, {variant}: лучше {better}, хуже {worse}, p={mcnemar(better, worse):.4f}")
    # ошибки по фото: сколько моделей ошиблись на этом снимке (как есть)
    keys = sorted({k for k, _, _ in flags})
    with (out / "errors_by_photo.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["фото", "товар", "ошиблись_моделей", "из"] + keys)
        for i, (o, p) in enumerate(items):
            fl = [flags.get((k, groups[i], "как есть"), {}).get(i) for k in keys]
            if any(x is False for x in fl):
                w.writerow([str(p), o, sum(x is False for x in fl), len(keys)] + [int(x is False) for x in fl])
    print(f"Готово: {out / 'results.csv'}, {out / 'errors_by_photo.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
