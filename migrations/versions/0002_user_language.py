"""Язык интерфейса в профиле сотрудника (ENGINEERING_STATUS §7, п. 7): язык следует за человеком на любое устройство.

Колонка необязательная (NULL — «не выбран», страница берёт язык браузера), поэтому миграция не трогает существующие строки и
обратима без потери чего-либо, кроме самого выбора языка.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("language", sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("language")
