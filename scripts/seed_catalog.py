"""
Наполняет локальную БД демонстрационными данными: магазин, несколько SKU,
простая планограмма на одну полку из 6 позиций. Полезно для быстрого
запуска API и ручного тестирования без реального каталога сети.

Запуск: python -m scripts.seed_catalog --yes-add-demo-data

ТОЛЬКО для пустой базы разработчика. На базе, где уже есть магазины/товары/пользователи, скрипт ОТКАЗЫВАЕТСЯ работать:
SKU уникальны, демо-данные конфликтовали бы с настоящими, а засорять рабочую базу демо-магазином нельзя.
Для настоящих данных: страница /ui/products.html, scripts.import_catalog.
"""
from __future__ import annotations

import sys

DEMO_FLAG = "--yes-add-demo-data"


def seed_blockers(counts: dict[str, int]) -> list[str]:
    """Причины, по которым демо-данные добавлять нельзя (чистая функция, тестируется офлайн)."""
    labels = {"stores": "магазины", "products": "товары (SKU)", "shelves": "полки", "planogram_slots": "позиции планограмм"}
    return [f"в базе уже есть {labels[k]}: {counts[k]}" for k in labels if counts.get(k, 0) > 0]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if DEMO_FLAG not in argv:
        print(f"Скрипт добавляет ДЕМОНСТРАЦИОННЫЕ данные (магазин «Демо-магазин №1», 3 SKU) и только в пустую базу. "
              f"Чтобы подтвердить: python -m scripts.seed_catalog {DEMO_FLAG}")
        return 1
    # Импорты БД — здесь, а не наверху: seed_blockers и проверка флага тестируются без SQLAlchemy.
    from app.db.models import PlanogramSlotORM, Product, ShelfORM, Store
    from app.db.session import SessionLocal, engine

    from app.db.migrate import ensure_current
    ensure_current(engine)          # миграции Alembic вместо create_all (шаг Р-2)
    db = SessionLocal()
    try:
        from sqlalchemy import func, select
        counts = {name: int(db.execute(select(func.count()).select_from(model)).scalar_one())
                  for name, model in (("stores", Store), ("products", Product), ("shelves", ShelfORM),
                                      ("planogram_slots", PlanogramSlotORM))}
        blockers = seed_blockers(counts)
        if blockers:
            print("Отказ: демо-данные добавляются только в ПУСТУЮ базу — " + "; ".join(blockers) + ".")
            print("Ничего не изменено. Для настоящих данных: /ui/products.html, scripts.import_catalog.")
            return 1
        store = Store(name="Демо-магазин №1", address="ул. Примерная, 1", format="supermarket")
        db.add(store)

        products = [
            Product(sku="COLA-0.5L", name="Напиток газированный, 0.5л", brand="DemoBrand",
                     category="Напитки", expected_price=89.90),
            Product(sku="COLA-1.5L", name="Напиток газированный, 1.5л", brand="DemoBrand",
                     category="Напитки", expected_price=169.90),
            Product(sku="CHIPS-CLASSIC", name="Чипсы классические", brand="DemoSnacks",
                     category="Снеки", expected_price=119.00),
        ]
        db.add_all(products)
        db.flush()

        shelf_id = "shelf-demo-1"
        db.add(ShelfORM(store_id=store.id, shelf_id=shelf_id, name="Демо-полка"))
        planogram = [
            PlanogramSlotORM(shelf_id=shelf_id, row=0, slot_index=0, expected_product_id=products[0].id),
            PlanogramSlotORM(shelf_id=shelf_id, row=0, slot_index=1, expected_product_id=products[0].id),
            PlanogramSlotORM(shelf_id=shelf_id, row=0, slot_index=2, expected_product_id=products[1].id),
            PlanogramSlotORM(shelf_id=shelf_id, row=1, slot_index=0, expected_product_id=products[2].id),
        ]
        db.add_all(planogram)

        db.commit()
        print(f"Готово: store_id={store.id}, shelf_id={shelf_id}, products={[p.sku for p in products]}")
        print("Дальше создайте пользователя (API закрыт авторизацией, F13): "
              "python -m scripts.create_user --username admin --role admin")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
