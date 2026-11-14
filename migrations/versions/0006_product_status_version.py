"""Товар: статус (в каталоге / в архиве), время изменения и версия (документ 06, §4.1 и §8 «Фундамент — нужно сейчас»).

* ``status`` — ``active`` | ``archived``. Товар в архиве не распознаётся, не находится по штрихкоду и не виден покупателю, но не
  удаляется: фото, штрихкоды и история остаются, товар можно вернуть. Удаления товара в программе нет намеренно.
* ``updated_at`` — когда товар меняли в последний раз (пусто — не меняли с добавления).
* ``version`` — номер правки: два сотрудника правят один товар — второй получает «товар уже изменили» вместо тихой перезаписи.

Существующие товары получают ``active`` и версию 1. Откат удаляет только эти столбцы (допустим на копии).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("status", sa.String(length=16), nullable=False, server_default="active"))
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.create_index("ix_products_status", ["status"])


def downgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.drop_index("ix_products_status")
        batch.drop_column("version")
        batch.drop_column("updated_at")
        batch.drop_column("status")
