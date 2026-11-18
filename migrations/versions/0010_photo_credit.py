"""Подпись источника фото товара (лицензия Open Food Facts — CC BY-SA — требует указать, чьё фото).

* ``product_images.credit`` — чьё фото, например «Open Food Facts, CC BY-SA»; пусто — своё фото магазина (подписи нет).

Существующие строки не меняются; подпись у уже загруженных фото ставит ``python -m scripts.set_photo_credit``.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-18
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("product_images") as batch:
        batch.add_column(sa.Column("credit", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("product_images") as batch:
        batch.drop_column("credit")
