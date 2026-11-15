"""Простой вход на этом компьютере (решение владельца 2026-11-15): ПИН-код и одноразовая ссылка «Придумайте ПИН».

* ``user_security``: ``pin_hash`` (scrypt, как пароль), ``pin_set_at``, ``pin_failures`` (ошибок подряд; при пределе ПИН снимается).
* ``auth_session_meta.method`` — «pin» у сеанса, открытого ПИН-кодом (у него свой срок простоя, app/core/local_pin.py).
* ``auth_setup_links`` — одноразовые ссылки первого входа и «забыли ПИН» (в базе только хеш).

Существующие строки не меняются: ПИН ни у кого не задан, прежний вход паролем и кодом работает как раньше.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_security") as batch:
        batch.add_column(sa.Column("pin_hash", sa.String(), nullable=True))
        batch.add_column(sa.Column("pin_set_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("pin_failures", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("auth_session_meta") as batch:
        batch.add_column(sa.Column("method", sa.String(), nullable=True))
    op.create_table(
        "auth_setup_links",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_auth_setup_links_user_id", "auth_setup_links", ["user_id"])
    op.create_index("ix_auth_setup_links_token_hash", "auth_setup_links", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_auth_setup_links_token_hash", table_name="auth_setup_links")
    op.drop_index("ix_auth_setup_links_user_id", table_name="auth_setup_links")
    op.drop_table("auth_setup_links")
    with op.batch_alter_table("auth_session_meta") as batch:
        batch.drop_column("method")
    with op.batch_alter_table("user_security") as batch:
        batch.drop_column("pin_failures")
        batch.drop_column("pin_set_at")
        batch.drop_column("pin_hash")
