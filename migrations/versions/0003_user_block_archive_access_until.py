"""Блокировка с причиной, архив и «доступ до» (документ 05, §12.1, §12.5, §12.7; app/core/account_status.py).

Все колонки необязательные: у существующих сотрудников ничего не меняется (заблокированные прежде — is_active = false — остаются
заблокированными, просто без причины). Обратима: откат теряет только причины, даты архива и сроки доступа.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("blocked_at", "block_reason", "archived_at", "access_until")


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("blocked_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("block_reason", sa.String(length=200), nullable=True))
        batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("access_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        for name in reversed(COLUMNS):
            batch.drop_column(name)
