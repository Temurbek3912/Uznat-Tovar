"""
Разнести товары по категориям по списку (задание владельца 2026-11-27): CSV «артикул;категория» -> правка базы.

    python -m scripts.set_categories plan.csv --db data/lite/shop.db            # только показать, что изменится
    python -m scripts.set_categories plan.csv --db data/lite/shop.db --apply    # копия базы рядом, затем правка

Категория должна быть из единого списка (app/services/categories.py) или «Без категории»; иначе строка не применяется — опечатка не
создаст новую категорию. Перед правкой — полная копия базы рядом с ней (shop.db.before-categories-<дата>); номер правки товара
(version) растёт, как при правке на странице. Программу можно не останавливать (база в режиме WAL).
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.services.categories import DEFAULT_CATEGORY, KIND_BY_RU


def read_plan(path: Path) -> list[tuple[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.reader(f, delimiter=";") if r and not r[0].startswith("#")]
    return [(r[0].strip(), r[1].strip()) for r in rows if len(r) >= 2 and r[0].strip() != "sku"]


def backup(db: Path) -> Path:
    target = db.with_name(f"{db.name}.before-categories-{datetime.now():%Y%m%d-%H%M%S}")
    src, dst = sqlite3.connect(db), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan", type=Path)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    allowed = set(KIND_BY_RU) | {DEFAULT_CATEGORY}
    con = sqlite3.connect(args.db)
    try:
        changes, problems = [], []
        for sku, category in read_plan(args.plan):
            row = con.execute("select id, name, category from products where sku = ?", (sku,)).fetchone()
            if row is None:
                problems.append(f"{sku}: товара нет")
            elif category not in allowed:
                problems.append(f"{sku}: «{category}» — не из списка категорий")
            elif row[2] != category:
                changes.append((row[0], sku, row[1], row[2], category))
        for _, sku, name, old, new in changes:
            print(f"{sku}  {name}: {old} -> {new}")
        for p in problems:
            print("НЕ ПРИМЕНЕНО:", p)
        print(f"Изменится: {len(changes)}; не применено: {len(problems)}")
        if not args.apply or not changes:
            return 0
        print("Копия базы:", backup(args.db))
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
        with con:
            for pid, _, _, _, new in changes:
                con.execute("update products set category = ?, version = coalesce(version, 1) + 1, updated_at = ? where id = ?",
                            (new, now, pid))
        print("Готово.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
