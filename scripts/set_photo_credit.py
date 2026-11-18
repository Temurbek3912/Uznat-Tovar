"""
Подпись источника у уже загруженных фото товаров: «Фото: Open Food Facts, CC BY-SA» (лицензия открытой базы требует указать,
чьё фото). Фото узнаются по содержимому (SHA-256 файла), а не по имени: из папок набора берутся все фото, и подпись ставится
тем записям product_images, у которых такой же SHA-256. Свои фото магазина (их нет в папках набора) остаются без подписи.

    python -m scripts.set_photo_credit --dir D:/uznat_tovar/catalog-sets/popular-500 --dir D:/uznat_tovar/catalog-sets/rich-pilot
    python -m scripts.set_photo_credit --dir ... --apply          # записать (без --apply — только показать, сколько)

Повторный запуск безопасен: фото, у которых подпись уже такая, не трогаются. Подпись другая (своя) — не перезаписывается
без --overwrite. Новые наборы: python -m scripts.import_catalog --dir ... --photo-credit "Open Food Facts, CC BY-SA".
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

OFF_CREDIT = "Open Food Facts, CC BY-SA"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def set_digests(dirs: list[Path]) -> set[str]:
    """SHA-256 всех фото наборов (<набор>/<артикул>/*.jpg)."""
    from app.services.product_images import sha256_file
    out = set()
    for d in dirs:
        for p in sorted(d.glob("*/*")):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                out.add(sha256_file(p))
    return out


def plan(rows, digests: set[str], credit: str, overwrite: bool = False) -> tuple[list, int, int]:
    """rows — записи product_images. Ответ: (кому поставить, уже с этой подписью, с другой подписью — пропущено)."""
    todo, same, other = [], 0, 0
    for row in rows:
        if row.sha256 not in digests:
            continue
        if row.credit == credit:
            same += 1
        elif row.credit and not overwrite:
            other += 1
        else:
            todo.append(row)
    return todo, same, other


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", action="append", required=True, help="папка набора фото (подпапка на артикул); можно несколько")
    ap.add_argument("--credit", default=OFF_CREDIT, help=f"подпись (по умолчанию «{OFF_CREDIT}»)")
    ap.add_argument("--apply", action="store_true", help="записать в базу (иначе только показать)")
    ap.add_argument("--overwrite", action="store_true", help="заменить и другую подпись")
    a = ap.parse_args(argv)
    credit = a.credit.strip()[:200]
    dirs = [Path(d) for d in a.dir]
    missing = [str(d) for d in dirs if not d.is_dir()]
    if missing or not credit:
        print("Нет папки: " + ", ".join(missing) if missing else "Пустая подпись.", file=sys.stderr)
        return 2
    digests = set_digests(dirs)
    from app.db.models import ProductImage
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        rows = db.query(ProductImage).all()
        todo, same, other = plan(rows, digests, credit, a.overwrite)
        print(f"Фото в наборах: {len(digests)}; записей в базе: {len(rows)}; поставить подпись: {len(todo)}; "
              f"уже подписаны: {same}; с другой подписью (пропуск): {other}")
        if not a.apply:
            print("Ничего не изменено (запустите с --apply).")
            return 0
        for row in todo:
            row.credit = credit
        db.commit()
        print(f"Готово: подпись «{credit}» у {len(todo)} фото.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
