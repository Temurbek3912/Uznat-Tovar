"""
Замер «вид товара» (документ 07, §10.7): насколько верно программа говорит «это напиток», «это обувь», «это лекарство».

    python -m ml.benchmark_kinds fetch  --out data/benchmark/kinds           # фото из открытых баз (Open Food/Beauty/Products/Pet Food Facts)
    python -m ml.benchmark_kinds run    --data data/benchmark/kinds --method vision        # большая модель (LM Studio)
    python -m ml.benchmark_kinds run    --data data/benchmark/kinds --method google/siglip2-base-patch16-224
    python -m ml.benchmark_kinds run    --data data/benchmark/kinds --method openai/clip-vit-base-patch32
    python -m ml.benchmark_kinds report --data data/benchmark/kinds --md docs/benchmark/kinds_report.md

Набор. Для каждого вида — товары из категории открытой базы (PLAN), фото лицевой стороны. Каждое фото в двух видах, как в замере
названий: ``orig`` (как в базе) и ``shopper`` (повёрнуто, мельче, темнее, размыто). Плюс ``negative`` — снимки без товара
(правильный ответ — «не товар»). Правильный вид — из категории базы; фото, где категория базы явно неверна (на фото другое),
помечаются вручную в ``labels.csv`` (колонка ``ok`` = 0) и в отчёт не идут.

Что считается для каждого способа и порога уверенности: сколько видов названо, сколько из них верно, доля ошибок среди названных
(с 95-процентным интервалом Уилсона) и доля «не назвал». Для малых моделей порог — доля уверенности 0..1; для большой — её ответ.
Фото не попадают в git (папка data/). Кадр уходит только на адрес модели из настроек (по умолчанию 127.0.0.1).
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
import urllib.parse
from pathlib import Path

from app.services.product_kinds import KIND_IDS, kind_for_categories
from ml.benchmark_naming import _full_size, _get_bytes, _get_json, make_negatives, make_shopper, wilson

OFF = "https://world.openfoodfacts.org"
OBF = "https://world.openbeautyfacts.org"
OPF = "https://world.openproductsfacts.org"
OPFF = "https://world.openpetfoodfacts.org"
PER_KIND = 12

# вид -> [(база, категория или "" — все товары базы)]; товары берутся по очереди из списка, пока не наберётся PER_KIND
PLAN: dict[str, list[tuple[str, str]]] = {
    "drink": [(OFF, "en:waters"), (OFF, "en:sodas"), (OFF, "en:fruit-juices"), (OFF, "en:energy-drinks"), (OFF, "en:beers")],
    "dairy": [(OFF, "en:milks"), (OFF, "en:yogurts"), (OFF, "en:cheeses"), (OFF, "en:butters")],
    "sweets": [(OFF, "en:chocolates"), (OFF, "en:biscuits"), (OFF, "en:candies")],
    "snack": [(OFF, "en:chips-and-fries"), (OFF, "en:salty-snacks"), (OFF, "en:nuts")],
    "tea_coffee": [(OFF, "en:teas"), (OFF, "en:coffees")],
    "grocery": [(OFF, "en:pastas"), (OFF, "en:rices"), (OFF, "en:flours"), (OFF, "en:breakfast-cereals")],
    "sauce": [(OFF, "en:ketchup"), (OFF, "en:mayonnaises"), (OFF, "en:sauces"), (OFF, "en:salts")],
    "canned": [(OFF, "en:canned-fishes"), (OFF, "en:canned-vegetables"), (OFF, "en:canned-foods")],
    "bakery": [(OFF, "en:breads")],
    "meat_fish": [(OFF, "en:sausages"), (OFF, "en:hams"), (OFF, "en:fishes")],
    "fruit_veg": [(OFF, "en:fresh-fruits"), (OFF, "en:fresh-vegetables")],
    "frozen": [(OFF, "en:ice-creams"), (OFF, "en:frozen-foods")],
    "baby": [(OFF, "en:baby-foods"), (OFF, "en:baby-milks"), (OPF, "en:diapers")],
    "medicine": [(OPF, "en:medicines")],
    "cosmetics": [(OBF, "en:shampoos"), (OBF, "en:toothpastes"), (OBF, "en:soaps"), (OBF, "en:deodorants")],
    "cleaning": [(OPF, "en:laundry-detergents"), (OPF, "en:cleaning-products")],
    "home": [(OPF, "en:batteries"), (OPF, "en:cookware"), (OPF, "en:light-bulbs")],
    "clothing": [(OPF, "en:clothing")],
    "footwear": [(OPF, "en:shoes")],
    "electronics": [(OPF, "en:smartphones"), (OPF, "en:electronics")],
    "stationery": [(OPF, "en:pens"), (OPF, "en:notebooks"), (OPF, "en:office-supplies")],
    "toy": [(OPF, "en:toys")],
    "pet": [(OPFF, "")],
    "tobacco": [(OPF, "en:cigarettes")],
}
VARIANTS = ("orig", "shopper")
MANIFEST = "manifest.csv"
LABELS = "labels.csv"


# ---------------------------------------------------------------- набор

def _search(base: str, category: str, page: int) -> list[dict]:
    query = {"fields": "code,product_name,brands,image_front_url,categories_tags", "page_size": 50, "page": page,
             "sort_by": "unique_scans_n"}
    if category:
        query["categories_tags"] = category
    return _get_json(f"{base}/api/v2/search?" + urllib.parse.urlencode(query)).get("products") or []


def _consistent(kind: str, base: str, tags: list[str]) -> bool:
    """Для продуктов питания: категории товара не противоречат виду (кефир в «напитках» — это молочное)."""
    if base != OFF:
        return True
    found = kind_for_categories(tags)
    return found in (None, kind)


def cmd_fetch(args) -> int:
    out = Path(args.out)
    for v in VARIANTS:
        (out / v).mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    seen: set[str] = set()
    kinds = args.kinds.split(",") if args.kinds else list(PLAN)
    for kind in kinds:
        got: list[dict] = []
        for base, category in PLAN[kind]:
            page = 1
            while len(got) < args.per_kind and page <= 3:
                try:
                    products = _search(base, category, page)
                except Exception as exc:  # noqa: BLE001
                    print(f"  {kind} {category}: {type(exc).__name__}")
                    break
                if not products:
                    break
                for p in products:
                    code = str(p.get("code") or "")
                    image = str(p.get("image_front_url") or "")
                    if not code or not image or code in seen or not _consistent(kind, base, p.get("categories_tags") or []):
                        continue
                    seen.add(code)
                    got.append({"code": code, "name": str(p.get("product_name") or ""), "image": _full_size(image),
                                "source": f"{base.split('//')[1].split('.')[1]}:{category or '*'}"})
                    if len(got) >= args.per_kind:
                        break
                page += 1
                time.sleep(7)                 # поиск открытых баз: не больше 10 запросов в минуту
            if len(got) >= args.per_kind:
                break
        saved = 0
        for p in got:
            try:
                raw = _get_bytes(p["image"])
            except Exception:  # noqa: BLE001
                try:
                    raw = _get_bytes(p["image"].replace(".full.jpg", ".400.jpg"))
                except Exception as exc:  # noqa: BLE001
                    print(f"  пропуск {p['code']}: {type(exc).__name__}")
                    continue
            name = f"{kind}_{p['code']}.jpg"
            (out / "orig" / name).write_bytes(raw)
            try:
                (out / "shopper" / name).write_bytes(make_shopper(raw, int(p["code"][-9:]) if p["code"][-9:].isdigit() else 1))
            except Exception:  # noqa: BLE001 — битое фото
                (out / "orig" / name).unlink(missing_ok=True)
                continue
            saved += 1
            for v in VARIANTS:
                rows.append({"file": f"{v}/{name}", "code": p["code"], "name": p["name"], "kind": kind, "variant": v,
                             "source": p["source"]})
        print(f"{kind}: {saved} товаров")
    rows += [{**r, "kind": "", "source": "synthetic"} for r in
             (dict(file=n["file"], code="", name="", variant="negative") for n in
              make_negatives(out / "negative", sorted((out / "orig").glob("*.jpg"))[::25]))]
    with open(out / MANIFEST, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["file", "code", "name", "kind", "variant", "source"])
        w.writeheader()
        w.writerows(rows)
    print(f"Набор: {len(rows)} фото -> {out / MANIFEST}")
    return 0


# ---------------------------------------------------------------- прогон

def _rows(data: Path) -> list[dict]:
    with open(data / MANIFEST, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _slug(method: str) -> str:
    return method.replace("/", "__")


def cmd_run(args) -> int:
    data = Path(args.data)
    rows = _rows(data)
    out_path = data / f"results_{_slug(args.method)}.csv"
    results = []
    started = time.time()
    if args.method == "vision":
        from app.core.config import get_settings
        from app.services.product_namer import answer_kind, ask_vision
        s = get_settings()
        for i, r in enumerate(rows):
            jpeg = (data / r["file"]).read_bytes()
            t0 = time.time()
            ans = ask_vision(jpeg, "en", s.naming_vision_url, s.naming_vision_model, 60.0)
            results.append({"file": r["file"], "pred": answer_kind(ans), "score": "",
                            "name": (ans or {}).get("name", ""), "confidence": (ans or {}).get("confidence", ""),
                            "raw_kind": (ans or {}).get("kind", ""), "seconds": round(time.time() - t0, 2)})
            if i % 50 == 0:
                print(f"  {i}/{len(rows)}")
    else:
        from PIL import Image
        from app.pipeline.kind_classifier import KindClassifier
        device = "cpu" if args.cpu else ("cuda" if _cuda() else "cpu")
        model = KindClassifier(args.method, Path(args.cache), device)
        for r in rows:
            t0 = time.time()
            with Image.open(data / r["file"]) as im:  # exif-ok: фото открытых баз, поворот не важен для вида
                res = model.classify_image(im.convert("RGB"))
            results.append({"file": r["file"], "pred": res.kind or "", "score": round(res.score, 4),
                            "name": "", "confidence": "", "raw_kind": res.runner_up or "",
                            "seconds": round(time.time() - t0, 3)})
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)
    print(f"{args.method}: {len(results)} ответов за {time.time() - started:.0f} с -> {out_path}")
    return 0


def _cuda() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- отчёт

def _labels(data: Path) -> dict[str, str]:
    """Ручная проверка: файл (orig/...) -> «0», если на фото не тот вид, что в категории базы (такие фото не считаются)."""
    path = data / LABELS
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        return {r["file"]: r.get("ok", "1") for r in csv.DictReader(f)}


def evaluate(rows: list[dict], preds: dict[str, dict], threshold: float | None, variant: str) -> dict:
    named = correct = none = 0
    negatives_named = 0
    part = [r for r in rows if r["variant"] == variant]
    for r in part:
        p = preds.get(r["file"]) or {}
        kind = p.get("pred") or ""
        if kind and threshold is not None and p.get("score") not in ("", None) and float(p["score"]) < threshold:
            kind = ""
        if variant == "negative":
            negatives_named += bool(kind)
            continue
        if not kind:
            none += 1
            continue
        named += 1
        correct += kind == r["kind"]
    wrong = named - correct
    lo, hi = wilson(wrong, named)
    return {"n": len(part), "named": named, "correct": correct, "wrong": wrong, "none": none, "neg_named": negatives_named,
            "risk": wrong / named if named else 0.0, "risk_lo": lo, "risk_hi": hi}


def per_kind(rows: list[dict], preds: dict[str, dict], threshold: float | None) -> dict[str, tuple[int, int, int]]:
    """вид -> (фото, верно, неверно) на всех вариантах кроме negative."""
    table: dict[str, list[int]] = {}
    for r in rows:
        if r["variant"] == "negative":
            continue
        p = preds.get(r["file"]) or {}
        kind = p.get("pred") or ""
        if kind and threshold is not None and p.get("score") not in ("", None) and float(p["score"]) < threshold:
            kind = ""
        t = table.setdefault(r["kind"], [0, 0, 0])
        t[0] += 1
        if kind:
            t[1 if kind == r["kind"] else 2] += 1
    return {k: tuple(v) for k, v in table.items()}


def cmd_report(args) -> int:
    data = Path(args.data)
    labels = _labels(data)
    rows = [r for r in _rows(data) if labels.get("orig/" + r["file"].split("/", 1)[1], "1") != "0" or r["variant"] == "negative"]
    lines = [f"Фото: {sum(r['variant'] == 'orig' for r in rows)} товаров x 2 варианта + "
             f"{sum(r['variant'] == 'negative' for r in rows)} без товара; исключено по ручной проверке: "
             f"{sum(v == '0' for v in labels.values())}.", ""]
    summary = {}
    methods: list[tuple[str, dict]] = []
    for path in sorted(data.glob("results_*.csv")):
        with open(path, encoding="utf-8") as f:
            methods.append((path.stem[len("results_"):].replace("__", "/"), {r["file"]: r for r in csv.DictReader(f)}))
    by_name = dict(methods)
    if "vision" in by_name and args.agree_with in by_name:
        # как в программе (product_namer._agreed_kind): вид показывается, только если обе модели назвали один и тот же вид
        small = by_name[args.agree_with]
        methods.append((f"vision+{args.agree_with} (согласие)", {
            f: {"pred": p["pred"] if p["pred"] and p["pred"] == (small.get(f) or {}).get("pred") else "", "score": "",
                "seconds": p.get("seconds", "")} for f, p in by_name["vision"].items()}))
    for method, preds in methods:
        secs = [float(p["seconds"]) for p in preds.values() if p.get("seconds")]
        thresholds = [None] if method.startswith("vision") else [0.0, 0.3, 0.4, 0.5, 0.6, 0.7]
        lines += [f"## {method}", "", f"Среднее время на фото: {sum(secs) / max(1, len(secs)):.2f} с", "",
                  "| порог | вариант | фото | назван вид | верно | неверно | ошибок среди названных (95 %) | не назвал | без товара: назван вид |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for th in thresholds:
            neg = evaluate(rows, preds, th, "negative")
            for v in VARIANTS:
                e = evaluate(rows, preds, th, v)
                summary[(method, th, v)] = e
                lines.append(f"| {'—' if th is None else th} | {v} | {e['n']} | {e['named']} | {e['correct']} | {e['wrong']} | "
                             f"{e['risk']:.0%} ({e['risk_lo']:.0%}–{e['risk_hi']:.0%}) | {e['none']} | "
                             f"{neg['neg_named']} из {neg['n']} |")
        th = None if method.startswith("vision") else args.kind_threshold
        lines += ["", f"По видам (порог {'—' if th is None else th}; фото — оба варианта): вид — фото / верно / неверно", ""]
        pk = per_kind(rows, preds, th)
        lines.append(", ".join(f"{k} {v[0]}/{v[1]}/{v[2]}" for k, v in sorted(pk.items(), key=lambda kv: KIND_IDS.index(kv[0]))))
        lines.append("")
    text = "\n".join(lines)
    if args.md:
        Path(args.md).write_text(text, encoding="utf-8")
    print(text)
    return 0


def cmd_confusions(args) -> int:
    """Список ошибок способа: файл, правильный вид, ответ — для ручной проверки фото."""
    data = Path(args.data)
    rows = {r["file"]: r for r in _rows(data)}
    with open(data / f"results_{_slug(args.method)}.csv", encoding="utf-8") as f:
        for p in csv.DictReader(f):
            r = rows[p["file"]]
            if r["variant"] != "negative" and p["pred"] and p["pred"] != r["kind"]:
                print(f"{p['file']}\t{r['kind']}\t-> {p['pred']}\t{p.get('score', '')}\t{r['name'][:40]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", default="data/benchmark/kinds")
    f.add_argument("--per-kind", type=int, default=PER_KIND)
    f.add_argument("--kinds", default="")
    r = sub.add_parser("run")
    r.add_argument("--data", default="data/benchmark/kinds")
    r.add_argument("--method", required=True)
    r.add_argument("--cache", default="data/models")
    r.add_argument("--cpu", action="store_true")
    rep = sub.add_parser("report")
    rep.add_argument("--data", default="data/benchmark/kinds")
    rep.add_argument("--md", default="")
    rep.add_argument("--kind-threshold", type=float, default=0.6)
    rep.add_argument("--agree-with", default="google/siglip2-base-patch16-224")
    c = sub.add_parser("confusions")
    c.add_argument("--data", default="data/benchmark/kinds")
    c.add_argument("--method", required=True)
    args = ap.parse_args(argv)
    return {"fetch": cmd_fetch, "run": cmd_run, "report": cmd_report, "confusions": cmd_confusions}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
