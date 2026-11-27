"""
Замер «было / стало» для дообученного поиска по виду (ml/finetune_embedder.py) и установка новой модели.

    .venv/Scripts/python.exe -m ml.finetune_check --dataset D:/uznat_tovar/kaggle-embedder/dataset --results <папка с cv_*.npy>
    .venv/Scripts/python.exe -m ml.finetune_check --install <lora_final.pt> --to models/embedder/siglip2-base-shop-ft

Замер — тот же, что у ml/train_projection.py --check-only, на тех же 1741 фото и тех же частях товаров: снимок — фото
товара, которого модель при обучении НЕ видела; каталог — все товары; копии снимка убраны по векторам исходной модели;
пороги «найдено» подбираются на другой невиденной части так, чтобы уверенных ошибок и принятых чужих было на 10 % меньше,
чем у программы без проекции. Сравниваются: как сейчас (SigLIP 2 + проекция), дообученная модель, дообученная + проекция.

--install вливает поправки LoRA в SigLIP 2 и сохраняет обычную модель (её читает app/pipeline/embedder.py по
models.embedder.weights_path) и сверяет векторы с моделью-с-поправками. config.yaml не меняет.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from ml.train_projection import PARTS, SAFETY, _pct, pick_thresholds, project, ranked_lists, tally, train_matrix

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read_dataset(folder: Path):
    with open(folder / "photos.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    owners = np.array([r["product"] for r in rows])
    front = np.array([r["front"] == "1" for r in rows])
    parts = np.array([int(r["part"]) for r in rows])
    return rows, owners, front, parts, np.load(folder / "base_vectors.npy")


def evaluate(vectors_of, raw, owners, front, parts, folds, now, unknown, limits, projection=False):
    """Честная проверка: vectors_of(k) — векторы всех фото моделью, не видевшей частей k и k+1."""
    cal_r, cal_s, test_r, test_s = {}, {}, {}, {}
    for k in folds:
        z = vectors_of(k)
        cal_q = np.where(parts == k)[0]
        test_q = np.where(parts == (k + 1) % PARTS)[0]
        if projection:
            train = np.where((parts != k) & (parts != (k + 1) % PARTS))[0]
            z = project(z, train_matrix(z[train], owners[train]))
        cal_r.update(ranked_lists(z, raw, owners, cal_q))
        cal_s.update(ranked_lists(z, raw, owners, cal_q, stranger=True))
        test_r.update(ranked_lists(z, raw, owners, test_q))
        test_s.update(ranked_lists(z, raw, owners, test_q, stranger=True))
    th = pick_thresholds(cal_r, cal_s, owners, front, unknown, *limits)
    return tally(test_r, test_s, owners, front, *(th or now), unknown), th


def row(title: str, c: Counter, th) -> str:
    t = f"{th[0]:.2f} / {th[1]:.2f}" if th else "—"
    return (f"| {title} | {_pct(c['top1'], c['n'])} | {_pct(c['top5'], c['n'])} | {_pct(c['front_top1'], c['front_n'])} | "
            f"{_pct(c['confident'], c['n'])} ({c['confident']}) | {c['wrong']} | {_pct(c['stranger_accepted'], c['strangers'])} "
            f"| {t} |")


def check(a) -> int:
    from app.core.config import get_settings  # noqa: PLC0415
    from ml.train_projection import base_thresholds  # noqa: PLC0415
    settings = get_settings()
    now, unknown = base_thresholds(settings), float(settings.matcher.unknown_threshold)
    rows, owners, front, parts, raw = read_dataset(Path(a.dataset))
    res = Path(a.results)
    folds = [k for k in range(PARTS) if (res / f"cv_{k}.npy").exists()]
    if not folds:
        print("Нет файлов cv_<k>.npy — сначала обучение с --cv")
        return 2
    # ограничения порогов — от программы без проекции (как у ml/train_projection.py)
    c0 = tally(ranked_lists(raw, raw, owners, range(len(owners))),
               ranked_lists(raw, raw, owners, range(len(owners)), stranger=True), owners, front, now[0], now[1], unknown)
    limits = (SAFETY * c0["wrong"] / c0["confident"], SAFETY * c0["stranger_accepted"] / max(1, c0["strangers"]))
    base_k = np.load(res / "base.npy").astype(np.float32) if (res / "base.npy").exists() else None
    lines = [f"Фото: {len(rows)}, товаров: {len(set(owners))}; частей проверки: {len(folds)} из {PARTS} "
             f"(замер на частях {', '.join(str((k + 1) % PARTS + 1) for k in folds)}).", "",
             "| Способ | Верный первым | В пятёрке | Первым, лицевые | «Найдено» | Из них неверно | Чужой принят | Пороги |",
             "|---|---|---|---|---|---|---|---|"]
    variants = [("SigLIP 2 без проекции", lambda k: raw, False),
                ("**Сейчас в программе: SigLIP 2 + проекция**", lambda k: raw, True)]
    if base_k is not None:
        cos = float(np.mean(np.sum(base_k * raw, axis=1)))
        print(f"Сверка: векторы по сжатым фото датасета и по исходным — средний косинус {cos:.3f}")
        variants.append(("SigLIP 2 + проекция, сжатые фото датасета", lambda k: base_k, True))
    variants += [("Дообученная", lambda k: np.load(res / f"cv_{k}.npy").astype(np.float32), False),
                 ("**Дообученная + проекция**", lambda k: np.load(res / f"cv_{k}.npy").astype(np.float32), True)]
    for title, fn, proj in variants:
        c, th = evaluate(fn, raw, owners, front, parts, folds, now, unknown, limits, proj)
        lines.append(row(title, c, th))
        print(lines[-1], flush=True)
    text = "\n".join(lines) + "\n"
    if a.report:
        Path(a.report).write_text(text, encoding="utf-8")
    print(text)
    return 0


def install(a) -> int:
    import torch  # noqa: PLC0415
    from transformers import AutoImageProcessor, SiglipVisionModel  # noqa: PLC0415

    from ml.finetune_embedder import add_lora, merge_lora  # noqa: PLC0415
    saved = torch.load(a.install, map_location="cpu", weights_only=False)
    hub, recipe, layers = saved["hub"], saved["recipe"], saved["layers"]
    processor = AutoImageProcessor.from_pretrained(hub)
    merged = SiglipVisionModel.from_pretrained(hub).eval()
    n = merge_lora(merged, layers)
    # сверка: модель с поправками (как при обучении) и обычная модель с влитыми поправками дают те же векторы
    lora = SiglipVisionModel.from_pretrained(hub).eval()
    add_lora(lora, recipe["rank"], recipe["alpha"])
    mods = dict(lora.named_modules())
    with torch.no_grad():
        for name, d in layers.items():
            mods[name].lora_a.copy_(d["a"].float())
            mods[name].lora_b.copy_(d["b"].float())
    rng = np.random.default_rng(0)
    imgs = [rng.integers(0, 255, (240, 200, 3), dtype=np.uint8) for _ in range(4)]
    x = processor(images=imgs, return_tensors="pt")
    with torch.no_grad():
        va = torch.nn.functional.normalize(lora(**x).pooler_output, dim=1)
        vb = torch.nn.functional.normalize(merged(**x).pooler_output, dim=1)
    cos = float((va * vb).sum(1).min())
    if cos < 0.999:
        print(f"Векторы не совпали (косинус {cos:.4f}) — модель не сохранена")
        return 1
    to = Path(a.to)
    to = to if to.is_absolute() else PROJECT_ROOT / to
    merged.save_pretrained(to)
    processor.save_pretrained(to)
    print(f"Влито слоёв: {n}; сверка векторов — косинус {cos:.5f}. Модель: {to}")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset")
    ap.add_argument("--results")
    ap.add_argument("--report", default="")
    ap.add_argument("--install", default="")
    ap.add_argument("--to", default="models/embedder/siglip2-base-shop-ft")
    a = ap.parse_args(argv)
    if a.install:
        return install(a)
    if not (a.dataset and a.results):
        ap.error("нужны --dataset и --results (или --install)")
    return check(a)


if __name__ == "__main__":
    raise SystemExit(main())
