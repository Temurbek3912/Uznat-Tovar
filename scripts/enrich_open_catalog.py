"""
Дополнить карточки товаров сведениями из открытых баз (Open Food / Beauty / Products Facts) по штрихкоду: вес/объём, страна,
производитель, состав, описание и категория (у товаров «Без категории»). Заполняются ТОЛЬКО пустые поля — то, что магазин
вписал сам, не меняется. Правила — app/services/open_product_details.py.

    python -m scripts.enrich_open_catalog                       # скачать недостающее и показать, что изменится (база не меняется)
    python -m scripts.enrich_open_catalog --apply               # записать в базу
    python -m scripts.enrich_open_catalog --apply --demo-prices # и ПРИМЕРНЫЕ цены + «в наличии» — ТОЛЬКО для тестового каталога

Наружу уходит только номер штрихкода. Ответы баз сохраняются в --cache (повторный запуск их не скачивает).
Перед --apply сделайте копию базы: python scripts/backup_db.py.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

from app.core.security import utcnow
from app.services import open_product_details as opd
from scripts.fetch_open_catalog import RU_KINDS, _get_json

BASES = ("https://world.openfoodfacts.org", "https://world.openbeautyfacts.org", "https://world.openproductsfacts.org")
FIELDS = ("code,product_name,product_name_ru,brands,brand_owner,quantity,categories_tags,generic_name,generic_name_ru,"
          "generic_name_uz,ingredients_text,ingredients_text_ru,ingredients_text_uz,ingredients_text_en")
NO_CATEGORY = ("", "Без категории")



def fetch_details(code: str) -> dict:
    """Карточка товара из первой базы, где он есть; нигде нет — {}."""
    for base in BASES:
        try:
            data = _get_json(f"{base}/api/v2/product/{code}.json?fields={FIELDS}", timeout=30, tries=2)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            continue
        if data.get("status") == 1 and data.get("product"):
            return data["product"]
    return {}


def category_for(p: dict, name: str) -> str:
    from app.services.product_kinds import kind_for_categories  # noqa: PLC0415
    kind = kind_for_categories(list(p.get("categories_tags") or []))
    return RU_KINDS.get(kind or "", "") or opd.category_from_name(name)


def plan_changes(product, code: str, p: dict, demo_prices: bool) -> dict:
    """Что дописать в товар (только пустые поля). product — объект с полями модели Product."""
    changes: dict = {}
    category = product.category
    if (category or "") in NO_CATEGORY:
        found = category_for(p, product.name)
        if found:
            changes["category"] = category = found
    for field, value in opd.details_for(p, code, product.name).items():
        if not (getattr(product, field) or "").strip():
            changes[field] = value
    brand = (getattr(product, "brand", "") or "").strip()
    if not (product.manufacturer or "").strip() and "manufacturer" not in changes and brand and brand != "Не указан":
        changes["manufacturer"] = brand                     # производитель неизвестен — хотя бы марка
    if demo_prices:
        if product.expected_price is None:
            net = changes.get("net_content") or product.net_content or ""
            changes["expected_price"] = float(opd.demo_price(category, net, code or product.sku))
        if product.in_stock is None:
            changes["in_stock"] = True
    return changes


NEEDED = ("category", "net_content", "expected_price", "country", "manufacturer")     # у каждого товара должно быть
FIX_FIELDS = ("name", "category", "net_content", "country", "manufacturer", "description")


def read_fixes(path: Path) -> dict[str, dict]:
    """Правки вручную (CSV, «;»): sku;name;category;net_content;country;manufacturer;description;archive.
    Заполненная ячейка заменяет значение (и найденное в открытой базе); archive=1 — убрать товар в архив (вернуть можно)."""
    import csv  # noqa: PLC0415
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {r["sku"].strip(): {k: (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f, delimiter=";")}


def apply_fix(product, changes: dict, fix: dict) -> dict:
    """Правка вручную поверх найденного: непустые ячейки побеждают."""
    for field in FIX_FIELDS:
        value = fix.get(field) or ""
        if value and value != (getattr(product, field) or ""):
            changes[field] = value
    if fix.get("archive") == "1" and product.status != "archived":
        changes["status"] = "archived"
    return changes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default="data/open-details.json")
    ap.add_argument("--apply", action="store_true", help="записать в базу (без него — только показать)")
    ap.add_argument("--demo-prices", action="store_true", help="ПРИМЕРНЫЕ цены и «в наличии» — только для тестового каталога")
    ap.add_argument("--offline", action="store_true", help="не ходить в интернет, только сохранённые ответы")
    ap.add_argument("--fixes", default="data/test-catalog-fixes.csv", help="правки вручную (см. read_fixes); нет файла — без них")
    args = ap.parse_args()

    from app.db.models import Product, ProductBarcodeORM  # noqa: PLC0415
    from app.db.session import SessionLocal  # noqa: PLC0415

    cache_path = Path(args.cache)
    cache: dict = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.status == "active").all()
        codes: dict[str, str] = {}
        for barcode, product_id in db.query(ProductBarcodeORM.barcode, ProductBarcodeORM.product_id).all():
            codes.setdefault(product_id, barcode)
        if not args.offline:
            missing = [c for c in codes.values() if c not in cache]
            print(f"Скачиваю сведения: {len(missing)} штрихкодов")
            for n, code in enumerate(missing, 1):
                found = fetch_details(code)
                if not found and len(code) == 13 and code.startswith("0"):      # UPC-A хранится у нас с нулём впереди
                    found = fetch_details(code[1:])
                cache[code] = found
                if n % 25 == 0 or n == len(missing):
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                    print(f"  {n}/{len(missing)}")
                time.sleep(0.7)
        from app.services import audit  # noqa: PLC0415
        fixes = read_fixes(Path(args.fixes))
        total: dict[str, int] = {}
        changed, gaps = 0, []
        for product in products:
            code = codes.get(product.id, "")
            fix = fixes.get(product.sku, {})
            view = SimpleNamespace(**{f: getattr(product, f) for f in (*FIX_FIELDS, "sku", "brand", "ingredients", "expected_price",
                                                                     "in_stock", "status")})
            view.name = fix.get("name") or view.name                     # исправленное название — для категории и веса
            view.category = fix.get("category") or view.category
            view.net_content = fix.get("net_content") or view.net_content  # для примерной цены
            changes = apply_fix(product, plan_changes(view, code, cache.get(code) or {}, args.demo_prices), fix)
            final = {f: changes.get(f, getattr(product, f)) for f in (*NEEDED, "status")}
            if final["status"] == "active":
                missing = [f for f in NEEDED if final[f] in (None, "", "Без категории")]
                if missing:
                    gaps.append(f"{product.sku}\t{view.name}\tнет: {', '.join(missing)}")
            if not changes:
                continue
            changed += 1
            for k in changes:
                total[k] = total.get(k, 0) + 1
            if args.apply:
                for k, v in changes.items():
                    setattr(product, k, v)
                if changes.get("status") == "archived":
                    audit.record(db, "catalog.product.archived", actor=audit.AuditActor(None, "console"), target_type="product",
                                 target_id=product.id, before={"status": "active"}, after={"status": "archived"},
                                 reason="тестовый каталог: мусорная запись открытой базы")
                product.version = (product.version or 1) + 1
                product.updated_at = utcnow()
        if args.apply:
            db.commit()
        print(("Записано" if args.apply else "Будет изменено") + f": товаров {changed} из {len(products)}")
        for k, v in sorted(total.items()):
            print(f"  {k}: {v}")
        if gaps:
            print(f"Чего-то не хватает у {len(gaps)} (впишите в --fixes):")
            print("\n".join(gaps))
    finally:
        db.close()


if __name__ == "__main__":
    main()
