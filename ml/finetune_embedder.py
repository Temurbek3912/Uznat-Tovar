"""
Дообучение поиска по виду (SigLIP 2 base 224, визуальная часть) на фото товаров магазина — на Kaggle (бесплатная видеокарта)
или на ноутбуке. Один файл без зависимостей от программы: на Kaggle его запускает ноутбук ml/kaggle/finetune.ipynb.

    python finetune_embedder.py --data <папка с photos.csv и images/> --out <папка результата> [--cv] [--final]

Датасет собирает ml/shop_dataset.py. Модель не переписывается целиком: к линейным слоям добавляются маленькие
поправки LoRA (ранг 16, ~2,7 млн чисел из 93 млн) — результат весит ~5 МБ, его быстро скачать; дома поправки
вливаются в веса модели (ml/finetune_check.py --install). Учится сближать разные фото одного товара и отдалять фото
разных товаров (контрастное обучение, как у проекции ml/train_projection.py, но внутри модели), на фото с искажениями
«как в магазине»: кадрирование, поворот, перспектива, свет, размытие.

--cv     честная проверка: 5 обучений, в каждом модель НЕ видит две части товаров (одна — для порогов, другая — для
         замера; части — те же, что у ml/train_projection.py, столбец part). Пишет cv_<k>.npy — векторы всех фото
         моделью k — и base.npy (векторы без дообучения по тем же сжатым фото). Замер считает дома ml/finetune_check.py.
--final  обучение на всех товарах — lora_final.pt (то, что ставится в программу, если проверка покажет выигрыш).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

HUB = "google/siglip2-base-patch16-224"
PARTS = 5
RECIPE = {"rank": 16, "alpha": 16, "epochs": 30, "products_per_batch": 48, "lr": 2e-4, "weight_decay": 0.01,
          "temperature": 0.05, "warmup": 20, "seed": 0}
TARGETS = ("q_proj", "k_proj", "v_proj", "out_proj", "fc1", "fc2")


# --------------------------------------------------------------------------- LoRA


class LoRALinear(nn.Module):
    """y = W·x + (alpha/r)·B·A·x; W заморожен, учатся только A и B (B в начале нулевая — модель как была)."""

    def __init__(self, base: nn.Linear, rank: int, alpha: float):
        super().__init__()
        self.base = base
        self.scale = alpha / rank
        self.lora_a = nn.Parameter(torch.empty(rank, base.in_features))
        self.lora_b = nn.Parameter(torch.zeros(base.out_features, rank))
        nn.init.kaiming_uniform_(self.lora_a, a=math.sqrt(5))

    def forward(self, x):
        return self.base(x) + F.linear(F.linear(x, self.lora_a.to(x.dtype)), self.lora_b.to(x.dtype)) * self.scale


def add_lora(model: nn.Module, rank: int, alpha: float) -> list[str]:
    for p in model.parameters():
        p.requires_grad_(False)
    names = []
    for name, module in list(model.named_modules()):
        if isinstance(module, nn.MultiheadAttention):    # голова MAP: её out_proj читается напрямую, обёртку не терпит
            continue
        for child_name, child in list(module.named_children()):
            if isinstance(child, nn.Linear) and child_name in TARGETS:
                setattr(module, child_name, LoRALinear(child, rank, alpha))
                names.append(f"{name}.{child_name}" if name else child_name)
    return names


def lora_state(model: nn.Module) -> dict:
    """Поправки слоёв: имя слоя -> (A, B, scale); fp16 — так файл вдвое меньше."""
    out = {}
    for name, module in model.named_modules():
        if isinstance(module, LoRALinear):
            out[name] = {"a": module.lora_a.detach().half().cpu(), "b": module.lora_b.detach().half().cpu(),
                         "scale": module.scale}
    return out


def merge_lora(model: nn.Module, state: dict) -> int:
    """Влить поправки в веса обычной модели (без LoRA): W += scale·B·A. Возвращает число изменённых слоёв."""
    modules = dict(model.named_modules())
    for name, d in state.items():
        lin = modules[name]
        delta = (d["b"].float() @ d["a"].float()) * float(d["scale"])
        with torch.no_grad():
            lin.weight += delta.to(lin.weight.dtype).to(lin.weight.device)
    return len(state)


# --------------------------------------------------------------------------- данные


def read_photos(data: Path) -> list[dict]:
    with open(data / "photos.csv", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["part"] = int(r["part"])
        r["front"] = int(r["front"])
    return rows


def train_transform():
    from torchvision import transforms as T  # noqa: PLC0415
    return T.Compose([
        T.RandomResizedCrop(224, scale=(0.55, 1.0), ratio=(0.7, 1.4), antialias=True),
        T.RandomApply([T.RandomRotation(12, fill=255)], p=0.5),
        T.RandomPerspective(0.25, p=0.4, fill=255),
        T.RandomApply([T.ColorJitter(0.35, 0.35, 0.25, 0.03)], p=0.8),
        T.RandomApply([T.GaussianBlur(5, (0.1, 1.8))], p=0.3),
        T.ToTensor(),
        T.Normalize([0.5] * 3, [0.5] * 3),
    ])


def load_images(data: Path, rows: list[dict]):
    from PIL import Image  # noqa: PLC0415
    out = []
    for r in rows:
        with Image.open(data / "images" / r["image"]) as im:
            out.append(im.convert("RGB"))
    return out


@torch.no_grad()
def embed_all(model, processor, images, device, batch: int = 64) -> np.ndarray:
    """Векторы так же, как в программе (app/pipeline/embedder.SiglipEmbedder): процессор модели, pooler_output, L2."""
    model.eval()
    vecs = []
    for i in range(0, len(images), batch):
        inputs = processor(images=images[i:i + batch], return_tensors="pt").to(device)
        with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=device == "cuda"):
            v = model(**inputs).pooler_output
        vecs.append(F.normalize(v.float(), dim=1).cpu().numpy())
    return np.concatenate(vecs).astype(np.float32)


# --------------------------------------------------------------------------- обучение


def supcon(z: torch.Tensor, labels: torch.Tensor, temperature: float) -> torch.Tensor:
    """Контрастная потеря с несколькими «своими» (фото того же товара в пакете)."""
    sim = z @ z.t() / temperature
    n = z.shape[0]
    eye = torch.eye(n, dtype=torch.bool, device=z.device)
    sim = sim.masked_fill(eye, -1e4)
    pos = (labels[:, None] == labels[None, :]) & ~eye
    log_prob = sim - torch.logsumexp(sim, dim=1, keepdim=True)
    return -(log_prob * pos).sum(1).div(pos.sum(1).clamp(min=1)).mean()


def train(images, rows, keep: list[int], device: str, recipe: dict, log=print):
    """LoRA-модель, обученная на фото с номерами keep. Возвращает (модель, процессор)."""
    from transformers import AutoImageProcessor, SiglipVisionModel  # noqa: PLC0415
    seed = recipe["seed"]
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    processor = AutoImageProcessor.from_pretrained(HUB)
    model = SiglipVisionModel.from_pretrained(HUB).to(device)
    add_lora(model, recipe["rank"], recipe["alpha"])
    model.to(device)
    # пересчёт промежуточных значений вместо хранения: пакет 96 фото помещается и в 8 ГБ ноутбука (на Kaggle 16 ГБ)
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    params = [p for p in model.parameters() if p.requires_grad]
    tf = train_transform()
    by_product: dict[str, list[int]] = {}
    for i in keep:
        by_product.setdefault(rows[i]["product"], []).append(i)
    products = sorted(by_product)
    label_of = {p: k for k, p in enumerate(products)}
    per_batch = min(recipe["products_per_batch"], len(products))
    steps_per_epoch = max(1, len(products) // per_batch)
    total = steps_per_epoch * recipe["epochs"]
    opt = torch.optim.AdamW(params, lr=recipe["lr"], weight_decay=recipe["weight_decay"])
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / recipe["warmup"]) * 0.5 * (1 + math.cos(math.pi * min(1.0, s / total))))
    scaler = torch.amp.GradScaler(enabled=device == "cuda")
    model.train()
    step, t0 = 0, time.time()
    for epoch in range(recipe["epochs"]):
        order = random.sample(products, len(products))
        for b in range(steps_per_epoch):
            chosen = order[b * per_batch:(b + 1) * per_batch]
            xs, ys = [], []
            for p in chosen:
                photos = by_product[p]
                pair = random.sample(photos, 2) if len(photos) >= 2 and random.random() < 0.7 else [random.choice(photos)] * 2
                for i in pair:
                    xs.append(tf(images[i]))
                    ys.append(label_of[p])
            x = torch.stack(xs).to(device)
            y = torch.tensor(ys, device=device)
            with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=device == "cuda"):
                z = model(pixel_values=x).pooler_output
            loss = supcon(F.normalize(z.float(), dim=1), y, recipe["temperature"])
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            sched.step()
            step += 1
        if epoch % 5 == 4 or epoch == recipe["epochs"] - 1:
            log(f"    эпоха {epoch + 1}/{recipe['epochs']}: потеря {loss.item():.3f}, {time.time() - t0:.0f} с")
    return model, processor


def main(argv=None) -> int:
    import sys  # noqa: PLC0415
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cv", action="store_true")
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--recipe", default="", help="JSON с изменениями рецепта, например {\"epochs\": 20}")
    ap.add_argument("--folds", default="", help="только эти части проверки, например 0,1 (для пробы)")
    a = ap.parse_args(argv)
    recipe = dict(RECIPE, **(json.loads(a.recipe) if a.recipe else {}))
    data, out = Path(a.data), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    rows = read_photos(data)
    images = load_images(data, rows)
    parts = np.array([r["part"] for r in rows])
    print(f"Фото: {len(rows)}, товаров: {len({r['product'] for r in rows})}, устройство: {device}; рецепт: {recipe}")
    (out / "recipe.json").write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
    if a.cv:
        from transformers import AutoImageProcessor, SiglipVisionModel  # noqa: PLC0415
        if not (out / "base.npy").exists():
            base = SiglipVisionModel.from_pretrained(HUB).to(device)
            np.save(out / "base.npy", embed_all(base, AutoImageProcessor.from_pretrained(HUB), images, device).astype(np.float16))
            del base
        folds = [int(k) for k in a.folds.split(",")] if a.folds else range(PARTS)
        for k in folds:
            hidden = {k, (k + 1) % PARTS}                     # часть для порогов и часть для замера — модель их не видит
            keep = [i for i in range(len(rows)) if parts[i] not in hidden]
            print(f"  проверка {k + 1} из {PARTS}: обучение на {len(keep)} фото")
            model, processor = train(images, rows, keep, device, recipe)
            np.save(out / f"cv_{k}.npy", embed_all(model, processor, images, device).astype(np.float16))   # вдвое меньше качать
            del model
            if device == "cuda":
                torch.cuda.empty_cache()
    if a.final:
        print("  обучение на всех товарах")
        model, _ = train(images, rows, list(range(len(rows))), device, recipe)
        torch.save({"hub": HUB, "recipe": recipe, "photos": len(rows), "layers": lora_state(model)}, out / "lora_final.pt")
    print("Готово:", sorted(p.name for p in out.iterdir()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
