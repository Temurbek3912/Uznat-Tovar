"""
Замер «назвать товар вне базы» локальной моделью «зрение + текст» (документ 07, §10.6, «Что не измерено»).

    python -m ml.benchmark_naming fetch  --out data/benchmark/naming            # набор из открытых фото Open Food Facts
    python -m ml.benchmark_naming run    --data data/benchmark/naming           # спросить модель о каждом фото
    python -m ml.benchmark_naming report --data data/benchmark/naming --md docs/benchmark/naming_report.md

Что считается. Каждый ответ модели — одно из трёх:
  * «назвал верно»   — названы марка и вид товара (вкус, объём, вариант не обязательны: покупателю нужно «что это»);
  * «назвал неверно» — названа другая марка или другой товар;
  * «честно не определил» — модель сказала «не товар», дала пустое название или уверенность ниже порога.
Для каждого порога уверенности (``high`` — только «уверена»; ``medium`` — «уверена» и «скорее да») — сколько принято, доля ошибок
среди принятых (главная цифра, цель ≤ 1 %, §5.2) и 95-процентный интервал Уилсона.

Набор. ``fetch`` берёт популярные товары из Open Food Facts (Узбекистан, Россия, весь мир) с фото лицевой стороны; товары тестовой
базы магазина (Nutella, Pringles, Prince, Coca-Cola) пропускаются. Наружу уходят только поисковые запросы к Open Food Facts.
Каждое фото проверяется в двух видах: ``orig`` (как в базе — ровно, светло) и ``shopper`` (как снимает покупатель: повёрнуто,
мельче, темнее, размыто, с фоном). Плюс ``negative`` — снимки без товара (заливка, шум, сильное размытие): правильный ответ —
«не определил».

Проверка «верно/неверно» сначала автоматическая (общее слово с маркой из базы, латиница и кириллица в одной записи), затем —
вручную: в ``results.csv`` колонка ``verdict`` (correct | wrong | пусто = автоматическая оценка). Отчёт берёт ручную оценку, если она есть.

Фото не попадают в git (папка data/). Кадр уходит только на адрес модели из config.yaml (по умолчанию 127.0.0.1).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

from app.services.product_namer import (
    CONFIDENCE_LEVELS, USER_AGENT, _pick_brand, _tokens, accept_answer, ask_vision,
)

OFF_SEARCH = "https://world.openfoodfacts.org/api/v2/search"
CATALOG_BRANDS = _tokens("Coca-Cola Nutella Ferrero Pringles Prince")   # тестовая база магазина — не «вне базы»
DEFAULT_PLAN = (("uzbekistan", 45), ("russia", 30), ("", 25))                     # "" — весь мир
MAX_PER_BRAND = 2
VARIANTS = ("orig", "shopper")
MANIFEST = "manifest.csv"
RESULTS = "results.csv"
THRESHOLDS = ("high", "medium")


# ---------------------------------------------------------------- набор

def _get_json(url: str, timeout: float = 30.0, attempts: int = 5) -> dict:
    """Поиск Open Food Facts бывает перегружен (503) — повторяем с растущей паузой."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT + " benchmark", "Accept": "application/json"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 — адрес фиксирован (Open Food Facts)
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts - 1:
                raise
            print(f"  Open Food Facts: {exc} — повтор через {15 * (attempt + 1)} с")
            time.sleep(15 * (attempt + 1))
    return {}


def _get_bytes(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT + " benchmark"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 — images.openfoodfacts.org
        return r.read()


def _full_size(url: str) -> str:
    """front_ru.22.400.jpg -> front_ru.22.full.jpg (полный размер: ближе к снимку телефона)."""
    return url[: -len(".400.jpg")] + ".full.jpg" if url.endswith(".400.jpg") else url


def pick_products(products: list[dict], taken_brands: dict[str, int], taken_codes: set[str], limit: int) -> list[dict]:
    """Из ответа поиска — товары с названием, маркой и фото, не из базы магазина, не больше MAX_PER_BRAND на марку."""
    out = []
    for p in products:
        if len(out) >= limit:
            break
        code = str(p.get("code") or "")
        name = str(p.get("product_name_ru") or p.get("product_name") or p.get("product_name_en") or "").strip()
        brands = str(p.get("brands") or "").strip()
        image = str(p.get("image_front_url") or "")
        if not (code and name and brands and image) or code in taken_codes:
            continue
        brand = _pick_brand(brands, name)
        brand_key = " ".join(sorted(_tokens(brand))) or brand.lower()
        if _tokens(f"{brand} {brands}") & CATALOG_BRANDS:
            continue
        if taken_brands.get(brand_key, 0) >= MAX_PER_BRAND:
            continue
        taken_brands[brand_key] = taken_brands.get(brand_key, 0) + 1
        taken_codes.add(code)
        out.append({"code": code, "name": name, "brand": brand, "brands_all": brands,
                    "name_en": str(p.get("product_name_en") or ""), "image_url": _full_size(image)})
    return out


def make_shopper(jpeg: bytes, seed: int) -> bytes:
    """Фото «как снимает покупатель»: поворот, товар мельче на фоне, темнее, размыто, сильное сжатие. Детерминировано по seed."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    rnd = random.Random(seed)
    with Image.open(io.BytesIO(jpeg)) as im:  # exif-ok: поворот по EXIF — следующей строкой
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((900, 900))
        im = im.rotate(rnd.uniform(-12, 12), expand=True, fillcolor=(120, 115, 105))
        scale = rnd.uniform(0.55, 0.75)
        w, h = im.size
        canvas = Image.new("RGB", (int(w / scale), int(h / scale)), tuple(rnd.randint(60, 170) for _ in range(3)))
        canvas.paste(im, (rnd.randint(0, canvas.width - w), rnd.randint(0, canvas.height - h)))
        canvas = ImageEnhance.Brightness(canvas).enhance(rnd.uniform(0.6, 0.85))
        canvas = canvas.filter(ImageFilter.GaussianBlur(rnd.uniform(1.0, 2.2)))
        out = io.BytesIO()
        canvas.save(out, format="JPEG", quality=55)
        return out.getvalue()


def make_negatives(folder: Path, sources: list[Path]) -> list[dict]:
    """Снимки без товара: заливка, шум, градиент, сильно размытые фото товаров. Правильный ответ — «не определил»."""
    from PIL import Image, ImageFilter
    rnd = random.Random(7)
    rows = []

    def save(name: str, im) -> None:
        im.save(folder / name, format="JPEG", quality=85)
        rows.append({"file": f"negative/{name}", "code": "", "name": "", "brand": "", "brands_all": "", "name_en": "",
                     "kind": "negative", "variant": "negative"})

    folder.mkdir(parents=True, exist_ok=True)
    for i, color in enumerate([(200, 200, 200), (40, 40, 45), (180, 150, 110)]):
        save(f"fill_{i}.jpg", Image.new("RGB", (800, 600), color))
    for i in range(3):
        save(f"noise_{i}.jpg", Image.effect_noise((800, 600), rnd.uniform(40, 90)).convert("RGB"))
    grad = Image.linear_gradient("L").resize((800, 600)).convert("RGB")
    save("gradient.jpg", grad)
    for i, src in enumerate(sources[:5]):
        with Image.open(src) as im:  # exif-ok: сильное размытие — поворот не важен
            save(f"blur_{i}.jpg", im.convert("RGB").filter(ImageFilter.GaussianBlur(40)))
    return rows


def cmd_fetch(args) -> int:
    out = Path(args.out)
    (out / "orig").mkdir(parents=True, exist_ok=True)
    (out / "shopper").mkdir(parents=True, exist_ok=True)
    taken_brands: dict[str, int] = {}
    taken_codes: set[str] = set()
    rows: list[dict] = []
    for country, limit in DEFAULT_PLAN:
        got: list[dict] = []
        page = 1
        while len(got) < limit and page <= 10:
            query = {"fields": "code,product_name,product_name_ru,product_name_en,brands,image_front_url",
                     "page_size": 50, "page": page, "sort_by": "unique_scans_n"}
            if country:
                query["countries_tags"] = f"en:{country}"
            data = _get_json(OFF_SEARCH + "?" + urllib.parse.urlencode(query))
            got += pick_products(data.get("products") or [], taken_brands, taken_codes, limit - len(got))
            page += 1
            time.sleep(7)                          # поиск Open Food Facts: не больше 10 запросов в минуту
        for p in got:
            try:
                raw = _get_bytes(p["image_url"])
            except Exception:  # noqa: BLE001 — полного размера нет: берём 400 точек
                try:
                    raw = _get_bytes(p["image_url"].replace(".full.jpg", ".400.jpg"))
                except Exception as exc:  # noqa: BLE001
                    print(f"  пропуск {p['code']}: {type(exc).__name__}")
                    continue
            (out / "orig" / f"{p['code']}.jpg").write_bytes(raw)
            (out / "shopper" / f"{p['code']}.jpg").write_bytes(make_shopper(raw, int(p["code"][-9:])))
            for variant in VARIANTS:
                rows.append({"file": f"{variant}/{p['code']}.jpg", "code": p["code"], "name": p["name"], "brand": p["brand"],
                             "brands_all": p["brands_all"], "name_en": p["name_en"], "kind": country or "world",
                             "variant": variant})
        print(f"{country or 'world'}: {len(got)} товаров")
    rows += make_negatives(out / "negative", sorted((out / "orig").glob("*.jpg")))
    with open(out / MANIFEST, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Набор: {len(rows)} фото -> {out / MANIFEST}")
    return 0


# ---------------------------------------------------------------- прогон

def auto_verdict(row: dict, answer: dict | None) -> str:
    """correct / wrong по общему слову с маркой (или названием) из базы; для negative любое названное — wrong."""
    if not answer or answer.get("is_product") is False or not str(answer.get("name") or "").strip():
        return "none"
    if row["kind"] == "negative":
        return "wrong"
    mine = _tokens(f"{answer.get('name', '')} {answer.get('brand', '')}")
    truth = _tokens(f"{row['brand']} {row['brands_all']}") or _tokens(f"{row['name']} {row['name_en']}")
    return "correct" if mine & truth else "wrong"


def cmd_run(args) -> int:
    from app.core.config import get_settings
    settings = get_settings()
    url = args.url or settings.naming_vision_url
    model = args.model or settings.naming_vision_model
    data = Path(args.data)
    with open(data / MANIFEST, encoding="utf-8") as f:
        manifest = list(csv.DictReader(f))
    if args.limit:
        manifest = manifest[: args.limit]
    out_rows = []
    started = time.time()
    for i, row in enumerate(manifest, 1):
        jpeg = (data / row["file"]).read_bytes()
        t0 = time.time()
        answer = ask_vision(jpeg, args.lang, url, model, args.timeout)
        seconds = time.time() - t0
        if answer is None:
            print(f"[{i}] {row['file']}: модель не ответила")
        result = {**row, "answer_name": (answer or {}).get("name", ""), "answer_brand": (answer or {}).get("brand", ""),
                  "is_product": (answer or {}).get("is_product", ""), "confidence": (answer or {}).get("confidence", ""),
                  "seconds": f"{seconds:.2f}", "auto": auto_verdict(row, answer), "verdict": ""}
        out_rows.append(result)
        print(f"[{i}/{len(manifest)}] {row['variant']:8} {row['brand'][:18]:18} -> {result['confidence']:6} "
              f"{result['answer_name'][:50]} ({result['auto']}, {seconds:.1f} с)")
    with open(data / getattr(args, 'results', RESULTS), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"Готово за {time.time() - started:.0f} с -> {data / getattr(args, 'results', RESULTS)}")
    return 0


def cmd_rescore(args) -> int:
    """Пересчитать автоматическую оценку (колонка auto) без повторного опроса модели; ручная оценка (verdict) не трогается."""
    data = Path(args.data)
    with open(data / getattr(args, 'results', RESULTS), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        answer = {"is_product": str(row.get("is_product")).lower() != "false", "name": row.get("answer_name", ""),
                  "brand": row.get("answer_brand", "")}
        row["auto"] = auto_verdict(row, answer)
    with open(data / getattr(args, 'results', RESULTS), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Пересчитано: {len(rows)}")
    return 0


# ---------------------------------------------------------------- отчёт

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def outcome(row: dict, threshold: str) -> str:
    """correct / wrong / none для порога: ниже порога — «не определил» (как в приложении, accept_answer)."""
    answer = {"is_product": str(row.get("is_product")).lower() != "false", "name": row.get("answer_name", ""),
              "brand": row.get("answer_brand", ""), "confidence": row.get("confidence", "")}
    if accept_answer(answer, threshold) is None:
        return "none"
    verdict = (row.get("verdict") or "").strip() or row.get("auto", "")
    return "correct" if verdict == "correct" else "wrong"


def summarize(rows: list[dict]) -> list[dict]:
    groups = [("orig", "фото из базы (ровно, светло)"), ("shopper", "«как снимает покупатель»"),
              ("negative", "снимки без товара")]
    table = []
    for variant, title in groups:
        part = [r for r in rows if r["variant"] == variant]
        if not part:
            continue
        for threshold in THRESHOLDS:
            res = [outcome(r, threshold) for r in part]
            correct, wrong, none = res.count("correct"), res.count("wrong"), res.count("none")
            accepted = correct + wrong
            lo, hi = wilson(wrong, accepted)
            table.append({"variant": variant, "title": title, "threshold": threshold, "n": len(part),
                          "correct": correct, "wrong": wrong, "none": none,
                          "risk": wrong / accepted if accepted else 0.0, "risk_lo": lo, "risk_hi": hi})
    return table


def cmd_report(args) -> int:
    data = Path(args.data)
    with open(data / getattr(args, 'results', RESULTS), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    conf = {lvl: sum(1 for r in rows if r["confidence"] == lvl) for lvl in CONFIDENCE_LEVELS}
    seconds = sorted(float(r["seconds"]) for r in rows if r["seconds"])
    lines = ["| Фото | Порог | Всего | Назвал верно | Назвал неверно | Не определил | Ошибок среди названных (95 % интервал) |",
             "|---|---|---|---|---|---|---|"]
    for t in summarize(rows):
        risk = f"{t['risk'] * 100:.1f} % ({t['risk_lo'] * 100:.1f}–{t['risk_hi'] * 100:.1f} %)" \
            if t["correct"] + t["wrong"] else "—"
        lines.append(f"| {t['title']} | {t['threshold']} | {t['n']} | {t['correct']} | {t['wrong']} | {t['none']} | {risk} |")
    text = "\n".join(lines)
    text += f"\n\nУверенность модели в ответах: high — {conf['high']}, medium — {conf['medium']}, low — {conf['low']}."
    if seconds:
        text += f" Время ответа: медиана {seconds[len(seconds) // 2]:.1f} с, максимум {seconds[-1]:.1f} с."
    manual = sum(1 for r in rows if (r.get("verdict") or "").strip())
    text += f" Проверено вручную: {manual} из {len(rows)} ответов."
    print(text)
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        wrong = [r for r in rows if outcome(r, "medium") == "wrong"]
        detail = ["", "### Ошибки (порог medium)", "", "| Фото | В базе Open Food Facts | Модель сказала | Уверенность |", "|---|---|---|---|"]
        for r in wrong:
            truth = f"{r['brand']} — {r['name']}" if r["brand"] else "(снимок без товара)"
            detail.append(f"| {r['file']} | {truth} | {r['answer_name']} | {r['confidence']} |")
        Path(args.md).write_text((args.header or "") + text + "\n" + "\n".join(detail) + "\n", encoding="utf-8")
        print(f"-> {args.md}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Замер «назвать товар вне базы» (документ 07, §10.6)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="скачать набор из Open Food Facts")
    f.add_argument("--out", default="data/benchmark/naming")
    r = sub.add_parser("run", help="спросить модель о каждом фото")
    r.add_argument("--data", default="data/benchmark/naming")
    r.add_argument("--lang", default="ru")
    r.add_argument("--url", default="")
    r.add_argument("--model", default="")
    r.add_argument("--timeout", type=float, default=60.0)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--results", default=RESULTS, help="файл ответов в папке набора (свой для каждой модели при сравнении)")
    s = sub.add_parser("rescore", help="пересчитать автоматическую оценку")
    s.add_argument("--data", default="data/benchmark/naming")
    s.add_argument("--results", default=RESULTS)
    p = sub.add_parser("report", help="таблица «верно / неверно / не определил» по порогам")
    p.add_argument("--data", default="data/benchmark/naming")
    p.add_argument("--md", default="")
    p.add_argument("--header", default="")
    p.add_argument("--results", default=RESULTS)
    args = ap.parse_args(argv)
    return {"fetch": cmd_fetch, "run": cmd_run, "rescore": cmd_rescore, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
