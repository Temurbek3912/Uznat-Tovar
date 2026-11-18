"""
Чистка названий и брендов товаров по списку (CSV) — например, после загрузки из открытой базы, где в название попали состав,
калории, арабский текст, опечатки, набор букв. Список составляет человек, владелец его одобряет; скрипт только применяет.

    python -m scripts.fix_product_names --csv docs/catalog_cleanup/names_2026-11-18.csv            # показать, что будет
    python -m scripts.fix_product_names --csv docs/catalog_cleanup/names_2026-11-18.csv --apply    # записать

Столбцы CSV: sku, action (rename | archive), old_name, new_name, old_brand, new_brand, why.
* rename — новое название (пусто — не менять) и новый бренд (пусто — не менять). Проверки те же, что на странице «Товары»:
  clean_name / clean_text и «нет ли уже товара с таким названием».
* archive — убрать товар в архив (как кнопка «В архив»: покупатель не видит, не распознаётся, можно вернуть); запись в журнале.
* Строка применяется, только если название товара в базе всё ещё равно old_name: если его успели поправить руками — пропуск.
Повторный запуск безопасен: уже применённые строки пропускаются («уже так»). На распознавание по фото не влияет.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Row:
    sku: str
    action: str
    old_name: str
    new_name: str
    old_brand: str
    new_brand: str
    why: str


def read_rows(path: Path) -> list[Row]:
    with path.open(encoding="utf-8", newline="") as f:
        return [Row(**{k: (v or "").strip() for k, v in r.items()}) for r in csv.DictReader(f)]


@dataclass
class Step:
    row: Row
    outcome: str                  # apply | same | skip
    note: str = ""
    name: str | None = None       # новое значение (уже очищенное)
    brand: str | None = None


def plan(rows: list[Row], products: dict[str, tuple[str, str, str, str]]) -> list[Step]:
    """products: sku -> (id, name, brand, status). Проверяет всё до записи; названия считаются уже с учётом правок выше."""
    from app.db.models import PRODUCT_ARCHIVED
    from app.services.catalog_upload import MAX_TEXT_LEN, InputError, clean_name, clean_text, find_duplicate
    names = {pid: name for pid, name, _b, _s in products.values()}
    steps: list[Step] = []
    for row in rows:
        product = products.get(row.sku)
        if product is None:
            steps.append(Step(row, "skip", "нет такого артикула"))
            continue
        pid, name, brand, status = product
        if row.action == "archive":
            if status == PRODUCT_ARCHIVED:
                steps.append(Step(row, "same", "уже в архиве"))
            elif name != row.old_name:
                steps.append(Step(row, "skip", f"название уже другое: «{name}»"))
            else:
                steps.append(Step(row, "apply"))
            continue
        if row.action != "rename":
            steps.append(Step(row, "skip", f"непонятное действие «{row.action}»"))
            continue
        try:
            new_name = clean_name(row.new_name) if row.new_name.strip() else None
            new_brand = clean_text(row.new_brand, label="бренд", max_len=MAX_TEXT_LEN) if row.new_brand.strip() else None
        except InputError as exc:
            steps.append(Step(row, "skip", str(exc)))
            continue
        if (new_name is None or new_name == name) and (new_brand is None or new_brand == brand):
            steps.append(Step(row, "same", "уже так"))
            continue
        if name != row.old_name:
            steps.append(Step(row, "skip", f"название уже другое: «{name}»"))
            continue
        if new_name is not None and new_name != name:
            dup = find_duplicate(new_name, [(i, n) for i, n in names.items() if i != pid])
            if dup:
                steps.append(Step(row, "skip", f"уже есть товар с таким названием: «{dup[1]}»"))
                continue
            names[pid] = new_name
        steps.append(Step(row, "apply", name=new_name if new_name != name else None,
                          brand=new_brand if new_brand != brand else None))
    return steps


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="список правок")
    ap.add_argument("--apply", action="store_true", help="записать в базу (иначе только показать)")
    a = ap.parse_args(argv)
    path = Path(a.csv)
    if not path.is_file():
        print(f"Нет файла: {path}", file=sys.stderr)
        return 2
    rows = read_rows(path)
    from datetime import datetime, timezone

    from app.db.models import PRODUCT_ACTIVE, PRODUCT_ARCHIVED, Product
    from app.db.session import SessionLocal
    from app.services import audit
    db = SessionLocal()
    try:
        all_products = db.query(Product).all()
        by_sku = {p.sku: p for p in all_products}
        steps = plan(rows, {p.sku: (p.id, p.name, p.brand, p.status or PRODUCT_ACTIVE) for p in all_products})
        for s in steps:
            r = s.row
            if s.outcome == "apply" and r.action == "archive":
                text = f"в архив: «{r.old_name}» — {r.why}"
            elif s.outcome == "apply":
                parts = []
                if s.name:
                    parts.append(f"«{r.old_name}» → «{s.name}»")
                if s.brand:
                    parts.append(f"бренд «{r.old_brand}» → «{s.brand}»")
                text = "; ".join(parts)
            else:
                text = f"{'уже так' if s.outcome == 'same' else 'ПРОПУСК'}: «{r.old_name}» — {s.note}"
            print(f"{r.sku}: {text}")
        todo = [s for s in steps if s.outcome == "apply"]
        skipped = sum(s.outcome == "skip" for s in steps)
        print(f"\nВсего строк: {len(steps)}; применить: {len(todo)}; уже так: {sum(s.outcome == 'same' for s in steps)}; "
              f"пропуск: {skipped}")
        if not a.apply:
            print("Ничего не изменено (запустите с --apply).")
            return 0
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        actor = audit.AuditActor(None, "console")
        for s in todo:
            p = by_sku[s.row.sku]
            if s.row.action == "archive":
                p.status = PRODUCT_ARCHIVED
                audit.record(db, "catalog.product.archived", actor=actor, target_type="product", target_id=p.id,
                             before={"status": PRODUCT_ACTIVE}, after={"status": PRODUCT_ARCHIVED}, reason=s.row.why)
            else:
                if s.name:
                    p.name = s.name
                if s.brand:
                    p.brand = s.brand
            p.version = (p.version or 1) + 1
            p.updated_at = now
        db.commit()
        print(f"Готово: изменено товаров — {len(todo)}.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
