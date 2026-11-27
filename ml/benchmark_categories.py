"""
Замер «категория нового товара» (задание владельца 2026-11-27): насколько верно программа сама ставит категорию.

    python -m ml.benchmark_categories --db data/lite/shop.db --index data/lite/index/catalog.faiss --media data/lite \
        --kind-model google/siglip2-base-patch16-224 --md docs/benchmark/categories_report.md

Как. Каждый товар каталога с категорией по очереди считается «новым» (leave-one-out): его соседи ищутся по его же векторам в индексе,
но сам он из ответа убран. Правильный ответ — его категория в базе. Считается для каждого способа: сколько категорий названо
(охват), сколько из названных верно (точность) и сколько верно от всех товаров.

Способы: название (правила по словам), соседи по фото (голосование), малая модель по первому фото, и их сочетание
(app/services/category_guess.guess_category — так работает программа). Большая модель «зрение + текст» здесь не замеряется
(нужен запущенный LM Studio и минуты на каждый кадр).

База и индекс только читаются: индекс копируется во временную папку и грузится оттуда.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

from app.services.categories import category_for_kind, is_known
from app.services.category_guess import guess_category, neighbor_vote, product_neighbors
from app.services.open_product_details import category_from_name

SEARCH_K = 40


def load_products(db_path: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute("select id, name, category from products where status = 'active'").fetchall()
        photos = {}
        for pid, path in con.execute("select product_id, storage_path from product_images where is_active = 1 "
                                     "order by created_at"):
            photos.setdefault(pid, path)
    finally:
        con.close()
    return [{"id": pid, "name": name, "category": cat or "", "photo": photos.get(pid)} for pid, name, cat in rows]


def load_index(index_path: Path):
    from app.pipeline.matcher import FaissCatalogIndex  # noqa: PLC0415
    tmp = Path(tempfile.mkdtemp(prefix="bench_cat_"))
    for src in (index_path, Path(str(index_path) + ".ids.json")):
        shutil.copy2(src, tmp / src.name)
    return FaissCatalogIndex.load(str(tmp / index_path.name))


def neighbors_for(index, product: dict, categories: dict[str, str]) -> list[tuple[str, float]]:
    vectors, _ = index.product_vectors(product["id"])
    hits: list[tuple[str, float]] = []
    for vec in vectors:
        hits.extend(index.search(vec, SEARCH_K))
    return product_neighbors(hits, categories, exclude=product["id"])


def kind_signal(classifier, media: Path, product: dict) -> dict[str, float] | None:
    """Уверенность малой модели по каждому виду для первого фото товара (None — нет модели или фото)."""
    if classifier is None or not product["photo"]:
        return None
    path = media / product["photo"]
    if not path.is_file():
        return None
    return classifier.classify_jpeg(path.read_bytes()).probs


def first_hit(name: str, neighbors, probs: dict[str, float] | None) -> str:
    """Для сравнения: «первое сработавшее» — название, иначе большинство соседей ≥ 60 %, иначе модель с уверенностью ≥ 0,6."""
    by_name = category_from_name(name)
    if by_name:
        return by_name
    vote = neighbor_vote(neighbors)
    if vote and vote[1] >= 0.6:
        return vote[0]
    if probs:
        kind = max(probs, key=probs.get)
        if probs[kind] >= 0.6:
            return category_for_kind(kind)
    return ""


def top_kind(probs: dict[str, float] | None, min_score: float = 0.0) -> str:
    if not probs:
        return ""
    kind = max(probs, key=probs.get)
    return category_for_kind(kind) if probs[kind] >= min_score else ""


def score(rows: list[dict], key: str) -> dict:
    named = [r for r in rows if r[key]]
    right = sum(1 for r in named if r[key] == r["category"])
    return {"total": len(rows), "named": len(named), "right": right}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--index", type=Path, required=True)
    ap.add_argument("--media", type=Path, required=True, help="папка, от которой считаются пути фото (storage_path)")
    ap.add_argument("--kind-model", default="", help="малая модель вида (пусто — без неё)")
    ap.add_argument("--kind-cache", type=Path, default=Path("data/models"), help="векторы описаний видов (naming_kind_cache_dir)")
    ap.add_argument("--md", type=Path, default=None)
    args = ap.parse_args(argv)

    products = load_products(args.db)
    categories = {p["id"]: p["category"] for p in products}
    known = [p for p in products if is_known(p["category"])]
    index = load_index(args.index)
    classifier = None
    if args.kind_model:
        from app.pipeline.kind_classifier import KindClassifier  # noqa: PLC0415
        classifier = KindClassifier(args.kind_model, args.kind_cache, "cpu")

    rows = []
    for p in known:
        nb = neighbors_for(index, p, categories)
        probs = kind_signal(classifier, args.media, p)
        vote = neighbor_vote(nb)
        guess = guess_category(p["name"], nb, probs)
        rows.append({
            "category": p["category"], "now": "", "name_rule": category_from_name(p["name"]),
            "neighbors": vote[0] if vote else "", "kind_any": top_kind(probs), "kind_sure": top_kind(probs, 0.6),
            "first_hit": first_hit(p["name"], nb, probs),
            "no_model": (g.category if (g := guess_category(p["name"], nb, None)) else ""),
            "combined": guess.category if guess else "", "source": guess.source if guess else "",
        })

    labels = {"now": "БЫЛО: программа не ставит категорию («Без категории»)", "name_rule": "Только название (правила по словам)",
              "neighbors": "Только соседи по фото (голосование)", "kind_any": "Только малая модель",
              "kind_sure": "Только малая модель, уверенность ≥ 0,6", "first_hit": "Первое сработавшее из трёх",
              "no_model": "СТАЛО без малой модели (название + соседи)", "combined": "СТАЛО: взвешенное сочетание трёх"}
    lines = ["| Способ | Назван | Верно из названных | Верно из всех |", "|---|---|---|---|"]
    for key, text in labels.items():
        if key.startswith("kind") and classifier is None:
            continue
        s = score(rows, key)
        prec = f"{100 * s['right'] / s['named']:.1f} %" if s["named"] else "—"
        lines.append(f"| {text} | {s['named']} из {s['total']} | {s['right']} ({prec}) | {100 * s['right'] / s['total']:.1f} % |")
    by_source = Counter(r["source"] for r in rows if r["combined"])
    wrong = Counter((r["category"], r["combined"]) for r in rows if r["combined"] and r["combined"] != r["category"])
    lines += ["", "Откуда ответ в сочетании: " + ", ".join(f"{k} — {v}" for k, v in by_source.most_common()),
              "", "Частые ошибки сочетания (в базе → названо): " + "; ".join(f"{a} → {b}: {n}" for (a, b), n in wrong.most_common(8))]
    head = ["# Замер: категория нового товара", "",
            f"Каталог: {len(known)} товаров с категорией, каждый по очереди — «новый» (сам он из поиска соседей убран). "
            f"Малая модель: {args.kind_model or 'не использовалась'}. Запуск: python -m ml.benchmark_categories (см. описание в файле).",
            "", "Оговорка: часть категорий каталога сама когда-то поставлена правилами по названию (импорт из открытых баз) — "
            "поэтому точность «только название» здесь немного завышена. Веса подобраны на этом же каталоге; при подборе на одной "
            "половине товаров и проверке на другой сочетание дало 87 % верных от всех (против 88 % здесь).", ""]
    text = "\n".join(head + lines)
    print(text)
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(text + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
