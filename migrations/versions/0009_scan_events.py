"""Сканы покупателей для помощника «почему не узнал» (решение владельца 2026-11-17; документ 16, §10.5).

* ``scan_events`` — одна строка на скан «Узнать товар» у покупателя: время, итог (matched | uncertain | not_in_catalog | unknown),
  способ (штрихкод или вид), товар, название или вид товара вне базы. Без кадра и без сведений о покупателе (D-10, D-10а).
  Строки старше 90 дней удаляются сами (app/services/scan_log.py).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scan_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("via", sa.String(length=8), nullable=True),
        sa.Column("product_id", sa.String(), nullable=True),
        sa.Column("guess_name", sa.String(length=120), nullable=True),
        sa.Column("guess_kind", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scan_events_at", "scan_events", ["at"])
    op.create_index("ix_scan_events_product_id", "scan_events", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_scan_events_product_id", table_name="scan_events")
    op.drop_index("ix_scan_events_at", table_name="scan_events")
    op.drop_table("scan_events")
