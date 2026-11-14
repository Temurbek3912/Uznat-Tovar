"""Фото товаров как сущность и кэш эмбеддингов (документ 06, §4.1; документ 05, §13.3; документ 07, §8).

Только новые таблицы: существующие таблицы и данные не меняются. Записи о фото, которые уже лежат файлами (data/catalog-uploads)
или есть в индексе (импорт), создаёт не миграция, а app/services/product_images.backfill (воркер при запуске, скрипт
scripts/register_photos.py): ей нужны папка фото и индекс, которых миграция не знает. Файлы при этом не трогаются.

Откат удаляет только эти таблицы (записи о фото и кэш векторов; сами файлы фото и индекс остаются) — допустим на копии.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_images",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("storage_path", sa.String(), nullable=True),
        sa.Column("original_name", sa.String(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "sha256", name="uq_product_images_product_sha256"),
    )
    op.create_index("ix_product_images_product_id", "product_images", ["product_id"])
    op.create_index("ix_product_images_source_ref", "product_images", ["source_ref"])
    op.create_table(
        "embedding_models",
        sa.Column("version", sa.String(length=80), nullable=False),
        sa.Column("signature_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("version"),
    )
    op.create_table(
        "image_embeddings",
        sa.Column("image_id", sa.String(), nullable=False),
        sa.Column("model_version", sa.String(length=80), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["image_id"], ["product_images.id"]),
        sa.ForeignKeyConstraint(["model_version"], ["embedding_models.version"]),
        sa.PrimaryKeyConstraint("image_id", "model_version"),
    )


def downgrade() -> None:
    op.drop_table("image_embeddings")
    op.drop_table("embedding_models")
    op.drop_index("ix_product_images_source_ref", table_name="product_images")
    op.drop_index("ix_product_images_product_id", table_name="product_images")
    op.drop_table("product_images")
