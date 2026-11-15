"""Карточка товара для покупателя: сведения о товаре и выбор полей магазином (шаг 3 владельца, 2026-11-15).

* ``products``: ``manufacturer`` (производитель), ``country`` (страна), ``net_content`` (вес/объём), ``ingredients`` (состав),
  ``description`` (описание), ``in_stock`` (есть в наличии; NULL — не указано). Все необязательные: существующие товары не меняются.
* ``stores.card_fields`` — какие поля карточки видит покупатель (JSON-список кодов, app/services/shop_card.py); NULL — набор
  по умолчанию. Срока годности нет ни в товаре, ни в списке полей — намеренно (он у каждой партии свой).

Откат удаляет только эти столбцы (вместе с введёнными сведениями — допустим на копии).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRODUCT_COLUMNS = (
    ("manufacturer", sa.String()), ("country", sa.String()), ("net_content", sa.String()),
    ("ingredients", sa.Text()), ("description", sa.Text()), ("in_stock", sa.Boolean()),
)


def upgrade() -> None:
    with op.batch_alter_table("products") as batch:
        for name, type_ in PRODUCT_COLUMNS:
            batch.add_column(sa.Column(name, type_, nullable=True))
    with op.batch_alter_table("stores") as batch:
        batch.add_column(sa.Column("card_fields", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("stores") as batch:
        batch.drop_column("card_fields")
    with op.batch_alter_table("products") as batch:
        for name, _type in reversed(PRODUCT_COLUMNS):
            batch.drop_column(name)
