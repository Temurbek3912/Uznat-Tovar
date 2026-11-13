"""
Настройка магазинов и полок (без правки кода и без демо-сидера).

  # 1) магазин и его полки (код полки глобально уникален: используйте «<магазин>-<полка>»):
  python -m scripts.setup_store store --name "Макро Чиланзар" --address "ул. Бунёдкор, 1" \
      --format supermarket --shelf chil-napitki-1="Напитки, стеллаж 1" --shelf chil-snacks-1="Снеки, стеллаж 1"

  # 2) добавить полку позже:
  python -m scripts.setup_store shelf --store-id <ID> --shelf chil-sok-1="Соки"

  # 3) посмотреть, что есть:
  python -m scripts.setup_store list
"""
from __future__ import annotations

import argparse
import sys

from app.db.models import ShelfORM, Store
from app.db.session import SessionLocal, engine


def _parse_shelf_arg(value: str) -> tuple[str, str]:
    code, sep, name = value.partition("=")
    code, name = code.strip(), name.strip()
    if not code or any(ch.isspace() for ch in code):
        raise argparse.ArgumentTypeError(f"Код полки не должен быть пустым или содержать пробелы: {value!r}")
    return code, (name if sep and name else code)


def cmd_store(args) -> int:
    db = SessionLocal()
    try:
        clash = [c for c, _ in args.shelf if db.query(ShelfORM).filter_by(shelf_id=c).first()]
        if clash:
            print(f"Ошибка: коды полок уже заняты: {', '.join(clash)}", file=sys.stderr)
            return 1
        store = Store(name=args.name, address=args.address, format=args.format)
        db.add(store)
        db.flush()
        for code, name in args.shelf:
            db.add(ShelfORM(store_id=store.id, shelf_id=code, name=name))
        db.commit()
        print(f"Магазин создан: store_id={store.id}")
        for code, name in args.shelf:
            print(f"  полка: shelf_id={code}  ({name})")
        return 0
    finally:
        db.close()


def cmd_shelf(args) -> int:
    db = SessionLocal()
    try:
        if db.get(Store, args.store_id) is None:
            print("Ошибка: магазин не найден", file=sys.stderr)
            return 1
        for code, name in args.shelf:
            if db.query(ShelfORM).filter_by(shelf_id=code).first():
                print(f"Ошибка: код полки {code!r} уже занят", file=sys.stderr)
                return 1
            db.add(ShelfORM(store_id=args.store_id, shelf_id=code, name=name))
        db.commit()
        print("Полки добавлены")
        return 0
    finally:
        db.close()


def cmd_list(_args) -> int:
    db = SessionLocal()
    try:
        # Раньше на пустой базе команда молчала — и нельзя было отличить «данных нет» от «ничего не сработало».
        print(f"База данных: {engine.url.render_as_string(hide_password=True)}")
        stores = db.query(Store).order_by(Store.name).all()
        if not stores:
            print("Магазинов в базе нет (подключение к базе работает, таблицы пусты).")
            print("Создать магазин: команда `store` этого скрипта (в «Узнать товар» магазины не нужны, это наследие аудита полок).")
            return 0
        print(f"Магазинов: {len(stores)}")
        for st in stores:
            print(f"{st.name}  store_id={st.id}")
            for sh in db.query(ShelfORM).filter_by(store_id=st.id).order_by(ShelfORM.name).all():
                print(f"    {sh.shelf_id}  ({sh.name})")
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("store", help="создать магазин с полками")
    p.add_argument("--name", required=True)
    p.add_argument("--address", required=True)
    p.add_argument("--format", default="supermarket", choices=["hypermarket", "supermarket", "convenience"])
    p.add_argument("--shelf", action="append", type=_parse_shelf_arg, default=[], metavar="КОД=Название")
    p.set_defaults(func=cmd_store)

    p = sub.add_parser("shelf", help="добавить полки в магазин")
    p.add_argument("--store-id", required=True)
    p.add_argument("--shelf", action="append", type=_parse_shelf_arg, required=True, metavar="КОД=Название")
    p.set_defaults(func=cmd_shelf)

    p = sub.add_parser("list", help="показать магазины и полки")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    from app.db.migrate import ensure_current
    ensure_current(engine)          # миграции Alembic вместо create_all (шаг Р-2)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
