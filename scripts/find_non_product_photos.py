"""
Поиск фото «не товара» среди эталонов: селфи, люди, комната, только штрихкод, размытый кадр. Такие фото попадают из открытой
базы (Open Food Facts) и мешают распознаванию: кадр покупателя с человеком в кадре может «узнаться» как товар.

    python -m scripts.find_non_product_photos scan --out D:\\uznat_tovar\\photo-check-2026-11-18      # найти и показать
    python -m scripts.find_non_product_photos apply --csv docs/catalog_cleanup/photos_2026-11-18.csv        # показать, что будет
    python -m scripts.find_non_product_photos scan --since 2026-11-19 --out D:\\uznat_tovar\\photo-check-new  # только новые фото
    python -m scripts.find_non_product_photos apply --csv docs/catalog_cleanup/photos_2026-11-18.csv --apply

scan — модель «картинка + текст» SigLIP 2 (та же, что у поиска по виду, уже на диске) сравнивает каждое фото с описаниями
товаров (все виды из app/services/product_kinds.PROMPTS) и с описаниями «не товар» (NOT_PRODUCT ниже). Ничего не меняет:
пишет список (CSV, столбец decision пустой) и страницу с миниатюрами для человека; решает человек. Столбец left — сколько
у товара останется фото в эталонах, если вывести все его подозрительные (0 — по виду товар узнаваться перестанет, только по
штрихкоду). Поиск лиц (OpenCV) пробовали 2026-11-18: 92 ложных срабатывания на лицах с упаковок — не используется.

apply — выводит из эталонов строки CSV с decision=deactivate (как кнопка «Убрать из эталонов» на странице товара:
PhotoRegistry.change(active=False) + запись catalog.photo.deactivated в журнале, via=console). Файл и запись о фото не
удаляются, вернуть можно кнопкой. Повторный запуск безопасен («уже выведено»).
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import io
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

PRODUCT = "product"

# группа -> описания «не товар» (только английский — так модель обучена)
NOT_PRODUCT: dict[str, tuple[str, ...]] = {
    "person": ("a selfie of a person", "a photo of a person's face", "a portrait of a man", "a portrait of a woman",
               "a photo of a child", "a group of people", "people in a classroom", "people sitting at tables"),
    "scene": ("a photo of a room", "a photo of a street", "a photo of a building", "a landscape", "a photo of a car",
              "an empty shelf", "a photo of a floor", "a photo of a wall"),
    "barcode": ("a close-up of a barcode", "a barcode on a white background"),
    "document": ("a screenshot of a phone screen", "a printed document", "a store receipt", "a handwritten note"),
    "bad": ("a blurry photo", "a very dark photo", "random noise", "a plain colored surface"),
}
GROUP_RU = {"person": "человек", "scene": "комната/улица", "barcode": "штрихкод", "document": "документ/экран",
            "bad": "размыто/темно", PRODUCT: "товар"}
COLUMNS = ("image_id", "sku", "product_name", "status", "file", "suspect", "group", "not_product", "left", "decision", "why")


def label_texts() -> tuple[list[str], list[str]]:
    """(группа каждого текста, тексты). Все виды товаров — одна группа «товар»."""
    from app.services.product_kinds import PROMPTS
    groups, texts = [], []
    for prompts in PROMPTS.values():
        for p in prompts:
            groups.append(PRODUCT)
            texts.append(f"This is a photo of {p}.")
    for group, prompts in NOT_PRODUCT.items():
        for p in prompts:
            groups.append(group)
            texts.append(f"This is a photo of {p}.")
    return groups, texts


@dataclass(frozen=True)
class Verdict:
    group: str            # самая вероятная группа (product или одна из NOT_PRODUCT)
    not_product: float    # доля «не товар» 0..1 (сумма вероятностей всех групп, кроме товара)


def verdict(groups: list[str], logits: np.ndarray) -> Verdict:
    """Логиты по каждому описанию -> лучшее описание в группе -> softmax по группам."""
    best: dict[str, float] = {}
    for g, v in zip(groups, np.asarray(logits, dtype=np.float64).tolist(), strict=True):
        if g not in best or v > best[g]:
            best[g] = v
    names = list(best)
    vals = np.array([best[n] for n in names])
    probs = np.exp(vals - vals.max())
    probs /= probs.sum()
    top = names[int(np.argmax(probs))]
    return Verdict(top, float(sum(p for n, p in zip(names, probs, strict=True) if n != PRODUCT)))


def is_suspect(v: Verdict, min_not_product: float = 0.5) -> bool:
    """Подозрительное: модель скорее видит «не товар» (решает человек)."""
    return v.not_product >= min_not_product


def photos_left(rows: list[dict]) -> None:
    """Заполнить left: сколько фото товара останется в эталонах, если вывести все его подозрительные."""
    total: dict[str, int] = {}
    for r in rows:
        if not r["suspect"]:
            total[r["sku"]] = total.get(r["sku"], 0) + 1
    for r in rows:
        r["left"] = total.get(r["sku"], 0)


# --------------------------------------------------------------------------- модель


class Scorer:
    def __init__(self, repo: str, device: str):
        import torch  # noqa: PLC0415
        from transformers import AutoModel, AutoProcessor  # noqa: PLC0415
        self.torch, self.device = torch, device
        self.model = AutoModel.from_pretrained(repo, local_files_only=True).eval().to(device)
        self.processor = AutoProcessor.from_pretrained(repo, local_files_only=True)
        self.groups, texts = label_texts()
        with torch.no_grad():
            enc = self.processor(text=texts, return_tensors="pt", padding="max_length", max_length=64, truncation=True)
            feats = self.model.get_text_features(**{k: v.to(device) for k, v in enc.items()})
            feats = getattr(feats, "pooler_output", feats)
            self.text = torch.nn.functional.normalize(feats, dim=-1)
        self.scale = float(self.model.logit_scale.exp().item())
        self.bias = float(self.model.logit_bias.item())

    def verdicts(self, images: list) -> list[Verdict]:
        torch = self.torch
        with torch.no_grad():
            inputs = self.processor(images=images, return_tensors="pt").to(self.device)
            feats = self.model.get_image_features(**inputs)
            feats = torch.nn.functional.normalize(getattr(feats, "pooler_output", feats), dim=-1)
            logits = (self.scale * feats @ self.text.T + self.bias).float().cpu().numpy()
        return [verdict(self.groups, row) for row in logits]


def load_image(path: Path):
    from PIL import Image, ImageOps  # noqa: PLC0415
    with Image.open(path) as im:  # exif-ok: сразу поворачивается по EXIF (exif_transpose)
        return ImageOps.exif_transpose(im).convert("RGB")


def thumb_data_uri(image, side: int = 220) -> str:
    im = image.copy()
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def write_sheet(path: Path, rows: list[dict], thumbs: dict[str, str]) -> None:
    """Страница для человека: подозрительные фото с номером строки, товаром и причиной."""
    cards = []
    for n, r in enumerate(rows, 1):
        why = GROUP_RU.get(r["group"], r["group"])
        if not r["left"]:
            why += ", <b>других фото нет</b>"
        cards.append(
            f'<figure><img src="{thumbs[r["image_id"]]}" alt=""><figcaption><b>№{n}</b> {html.escape(r["product_name"])}'
            f'<br><small>{html.escape(r["sku"])}{" · в архиве" if r["status"] == "archived" else ""} · {why} · '
            f'не товар {float(r["not_product"]):.0%}</small></figcaption></figure>')
    path.write_text(
        "<!doctype html><meta charset=utf-8><title>Фото не товара</title><style>body{font:14px sans-serif;margin:16px;"
        "background:#fff;color:#111}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}"
        "figure{margin:0;border:1px solid #ccc;padding:6px;border-radius:6px}img{width:100%;height:220px;object-fit:contain;"
        "background:#f4f4f4}</style><h1>Подозрительные фото: " + str(len(rows)) + "</h1><main>" + "".join(cards) + "</main>",
        encoding="utf-8")


def scan(out: Path, csv_path: Path, min_not_product: float, batch: int, since: str = "") -> int:
    from app.core.config import get_settings
    from app.db.models import Product, ProductImage
    from app.db.session import SessionLocal
    from app.pipeline.embedder import SIGLIP2_HUB_MODEL
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        print("Нет torch: запустите из .venv программы.", file=sys.stderr)
        return 2
    media = Path(get_settings().media_root)
    db = SessionLocal()
    try:
        products = {p.id: p for p in db.query(Product).all()}
        query = db.query(ProductImage).filter(ProductImage.is_active.is_(True))
        if since:                                   # только фото, добавленные с этого дня (после новой загрузки)
            from datetime import datetime  # noqa: PLC0415
            query = query.filter(ProductImage.created_at >= datetime.fromisoformat(since))
        images = query.order_by(ProductImage.product_id, ProductImage.created_at).all()
    finally:
        db.close()
    print(f"Фото в эталонах: {len(images)}; модель {SIGLIP2_HUB_MODEL} ({device})")
    scorer = Scorer(SIGLIP2_HUB_MODEL, device)
    rows, thumbs, missing = [], {}, 0
    for start in range(0, len(images), batch):
        part, loaded = images[start:start + batch], []
        for img in part:
            path = media / (img.storage_path or "")
            if not img.storage_path or not path.is_file():
                missing += 1
                continue
            loaded.append((img, path, load_image(path)))
        for (img, path, im), v in zip(loaded, scorer.verdicts([x[2] for x in loaded]), strict=True):
            p = products.get(img.product_id)
            suspect = is_suspect(v, min_not_product)
            row = {"image_id": img.id, "sku": p.sku if p else "", "product_name": p.name if p else "", "status": p.status if p else "",
                   "file": img.storage_path, "suspect": int(suspect), "group": v.group, "not_product": f"{v.not_product:.3f}",
                   "left": 0, "decision": "", "why": ""}
            rows.append(row)
            if suspect:
                thumbs[img.id] = thumb_data_uri(im)
        print(f"  {min(start + batch, len(images))}/{len(images)}", flush=True)
    photos_left(rows)
    rows.sort(key=lambda r: (-r["suspect"], -float(r["not_product"])))
    suspects = [r for r in rows if r["suspect"]]
    out.mkdir(parents=True, exist_ok=True)
    write_sheet(out / "photos.html", suspects, thumbs)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(suspects)
    with (out / "all_photos.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"Подозрительных: {len(suspects)} из {len(rows)} (нет файла: {missing}).")
    print(f"Страница: {out / 'photos.html'}\nСписок: {csv_path} (решение — столбец decision: deactivate)")
    return 0


# --------------------------------------------------------------------------- применение


def plan(rows: list[dict], images: dict[str, tuple[str, bool]]) -> list[tuple[dict, str]]:
    """images: image_id -> (product_id, активно ли). Возвращает (строка, итог): apply | same | skip:<причина>.
    Строки без decision=deactivate не трогаются."""
    out = []
    for r in rows:
        if (r.get("decision") or "").strip().lower() != "deactivate":
            continue
        info = images.get(r["image_id"])
        if info is None:
            out.append((r, "skip:нет такого фото"))
        elif not info[1]:
            out.append((r, "same"))
        else:
            out.append((r, "apply"))
    return out


def apply(csv_path: Path, do_apply: bool) -> int:
    from app.db.models import ProductImage
    from app.db.session import SessionLocal
    from app.services import audit
    from app.services.product_images import PhotoRegistry
    with csv_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    db = SessionLocal()
    try:
        images = {i: (pid, bool(a)) for i, pid, a in db.query(ProductImage.id, ProductImage.product_id, ProductImage.is_active)}
        steps = plan(rows, images)
        for r, outcome in steps:
            text = {"apply": "вывести из эталонов", "same": "уже выведено"}.get(outcome, "ПРОПУСК — " + outcome[5:])
            print(f"{r['sku']} «{r['product_name']}» {r['file']}: {text}")
        todo = [r for r, o in steps if o == "apply"]
        print(f"\nК выводу: {len(todo)}; уже выведено: {sum(o == 'same' for _r, o in steps)}; "
              f"пропуск: {sum(o.startswith('skip') for _r, o in steps)}")
        if not do_apply:
            print("Ничего не изменено (запустите с --apply).")
            return 0
        reg, actor = PhotoRegistry(db), audit.AuditActor(None, "console")
        for r in todo:
            pid = images[r["image_id"]][0]
            _row, before, after = reg.change(r["image_id"], active=False, product_id=pid)
            if "active" in after:
                audit.record(db, "catalog.photo.deactivated", actor=actor, target_type="product", target_id=pid,
                             before={"image_id": r["image_id"], "active": before["active"]},
                             after={"image_id": r["image_id"], "active": after["active"]},
                             reason=(r.get("why") or "").strip() or "фото не товара (проверка фото)")
        db.commit()
        print(f"Готово: выведено из эталонов — {len(todo)}. Файлы не удалены.")
        return 0
    finally:
        db.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="найти подозрительные фото (ничего не меняет)")
    s.add_argument("--out", required=True, help="папка для страницы с миниатюрами (на D)")
    s.add_argument("--csv", default="docs/catalog_cleanup/photos_check.csv", help="список подозрительных")
    s.add_argument("--min-not-product", type=float, default=0.5)
    s.add_argument("--batch", type=int, default=32)
    s.add_argument("--since", default="", help="только фото, добавленные с этого дня (ГГГГ-ММ-ДД), — проверка после загрузки; "
                   "left тогда считается только по новым фото")
    a = sub.add_parser("apply", help="вывести из эталонов строки с decision=deactivate")
    a.add_argument("--csv", required=True)
    a.add_argument("--apply", action="store_true", help="записать в базу (иначе только показать)")
    args = ap.parse_args(argv)
    if args.cmd == "scan":
        return scan(Path(args.out), Path(args.csv), args.min_not_product, args.batch, args.since)
    path = Path(args.csv)
    if not path.is_file():
        print(f"Нет файла: {path}", file=sys.stderr)
        return 2
    return apply(path, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
