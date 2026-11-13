"""Базовая ревизия: схема базы на 2026-11-13 (все таблицы, в том числе архивные таблицы аудита полок и таблицы входа).

Что делает upgrade:
  * пустая база — создаёт все таблицы и индексы;
  * старая база, созданная до миграций (scripts/bootstrap.py через create_all), — создаёт ТОЛЬКО недостающие таблицы; существующие
    таблицы и данные не трогает. Перед этим bootstrap проверяет, что в существующих таблицах есть все колонки BASELINE_COLUMNS
    (app/db/migrate.py); если нет — останавливается, ничего не меняя.

downgrade запрещён: откат ниже базовой ревизии означает удаление всех таблиц с данными. Восстановление — из резервной копии.

Состав зафиксирован и от моделей не зависит: дальнейшие изменения схемы — только новыми ревизиями.

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Колонки каждой таблицы базовой схемы — для проверки старой базы перед принятием (app/db/migrate.py).
BASELINE_COLUMNS: dict[str, frozenset[str]] = {
    'agent_state': frozenset(['cameras_count', 'last_seen_at', 'name', 'version']),
    'api_tokens': frozenset(['created_at', 'expires_at', 'id', 'token_hash', 'user_id']),
    'auth_mfa_challenges': frozenset(['created_at', 'expires_at', 'failed_attempts', 'id', 'pass_hash', 'used_at', 'user_id']),
    'auth_recovery_codes': frozenset(['code_hash', 'created_at', 'id', 'used_at', 'user_id']),
    'auth_session_meta': frozenset(['auth_at', 'created_at', 'device_browser', 'device_kind', 'device_label', 'device_os', 'ip_hash', 'last_used_at', 'listener', 'mfa', 'reauth_at', 'scope', 'session_epoch', 'source', 'token_id', 'user_id']),
    'camera_events': frozenset(['camera_id', 'created_at', 'id', 'kind', 'message', 'snapshot_id']),
    'cameras': frozenset(['capture_requested_at', 'created_at', 'enabled', 'id', 'interval_seconds', 'kind', 'last_attempt_at', 'last_error', 'last_error_at', 'last_snapshot_id', 'last_success_at', 'last_test_at', 'last_test_message', 'last_test_ok', 'name', 'shelf_id', 'source_enc', 'source_masked', 'store_id', 'test_requested_at', 'updated_at', 'verify_tls']),
    'compliance_reports': frozenset(['compliance_score', 'created_at', 'empty_slots_json', 'id', 'osa_score', 'snapshot_id', 'violations_json']),
    'detections': frozenset(['detected_price', 'detector_confidence', 'id', 'match_score', 'matched_product_id', 'price_diff_pct', 'price_status', 'review_action', 'reviewed_at', 'reviewed_by', 'shelf_row', 'slot_index', 'snapshot_id', 'status', 'x1', 'x2', 'y1', 'y2']),
    'planogram_slots': frozenset(['expected_facings', 'expected_product_id', 'id', 'row', 'shelf_id', 'slot_index']),
    'product_barcodes': frozenset(['barcode', 'created_at', 'product_id']),
    'products': frozenset(['brand', 'category', 'created_at', 'embedding_index_ref', 'expected_price', 'id', 'name', 'sku']),
    'shelf_snapshots': frozenset(['created_at', 'id', 'image_path', 'shelf_id', 'status', 'store_id']),
    'shelves': frozenset(['id', 'name', 'shelf_id', 'store_id']),
    'stores': frozenset(['address', 'format', 'id', 'name']),
    'user_security': frozenset(['is_owner', 'must_change_password', 'session_epoch', 'temp_password_expires_at', 'totp_enabled_at', 'totp_last_step', 'totp_pending_enc', 'totp_secret_enc', 'updated_at', 'user_id']),
    'user_store_access': frozenset(['store_id', 'user_id']),
    'users': frozenset(['created_at', 'id', 'is_active', 'password_hash', 'role', 'username']),
}

_created: set[str] = set()


def _table(name: str, *elements) -> None:
    if sa.inspect(op.get_bind()).has_table(name):
        return                                  # таблица из старой базы: не трогаем
    op.create_table(name, *elements)
    _created.add(name)


def _index(name: str, table: str, columns: list[str], *, unique: bool) -> None:
    if table in _created:                       # у старых таблиц индексы уже созданы create_all
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    _created.clear()
    _table('agent_state',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False),
        sa.Column('version', sa.String(), nullable=True),
        sa.Column('cameras_count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('name')
    )
    _table('products',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('sku', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('brand', sa.String(), nullable=False),
        sa.Column('category', sa.String(), nullable=False),
        sa.Column('expected_price', sa.Float(), nullable=True),
        sa.Column('embedding_index_ref', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    _index('ix_products_sku', 'products', ['sku'], unique=True)
    _table('stores',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('address', sa.String(), nullable=False),
        sa.Column('format', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    _table('users',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('username', sa.String(), nullable=False),
        sa.Column('password_hash', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    _index('ix_users_username', 'users', ['username'], unique=True)
    _table('api_tokens',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    _index('ix_api_tokens_token_hash', 'api_tokens', ['token_hash'], unique=True)
    _index('ix_api_tokens_user_id', 'api_tokens', ['user_id'], unique=False)
    _table('auth_mfa_challenges',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('pass_hash', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('failed_attempts', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    _index('ix_auth_mfa_challenges_pass_hash', 'auth_mfa_challenges', ['pass_hash'], unique=True)
    _index('ix_auth_mfa_challenges_user_id', 'auth_mfa_challenges', ['user_id'], unique=False)
    _table('auth_recovery_codes',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('code_hash', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    _index('ix_auth_recovery_codes_code_hash', 'auth_recovery_codes', ['code_hash'], unique=False)
    _index('ix_auth_recovery_codes_user_id', 'auth_recovery_codes', ['user_id'], unique=False)
    _table('cameras',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('source_enc', sa.Text(), nullable=False),
        sa.Column('source_masked', sa.String(), nullable=False),
        sa.Column('store_id', sa.String(), nullable=False),
        sa.Column('shelf_id', sa.String(), nullable=False),
        sa.Column('interval_seconds', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('verify_tls', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_success_at', sa.DateTime(), nullable=True),
        sa.Column('last_error_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_snapshot_id', sa.String(), nullable=True),
        sa.Column('last_test_at', sa.DateTime(), nullable=True),
        sa.Column('last_test_ok', sa.Boolean(), nullable=True),
        sa.Column('last_test_message', sa.Text(), nullable=True),
        sa.Column('capture_requested_at', sa.DateTime(), nullable=True),
        sa.Column('test_requested_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )
    _index('ix_cameras_shelf_id', 'cameras', ['shelf_id'], unique=False)
    _index('ix_cameras_store_id', 'cameras', ['store_id'], unique=False)
    _table('planogram_slots',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('shelf_id', sa.String(), nullable=False),
        sa.Column('row', sa.Integer(), nullable=False),
        sa.Column('slot_index', sa.Integer(), nullable=False),
        sa.Column('expected_product_id', sa.String(), nullable=False),
        sa.Column('expected_facings', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['expected_product_id'], ['products.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    _index('ix_planogram_slots_shelf_id', 'planogram_slots', ['shelf_id'], unique=False)
    _table('product_barcodes',
        sa.Column('barcode', sa.String(), nullable=False),
        sa.Column('product_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('barcode')
    )
    _index('ix_product_barcodes_product_id', 'product_barcodes', ['product_id'], unique=False)
    _table('shelf_snapshots',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('store_id', sa.String(), nullable=False),
        sa.Column('shelf_id', sa.String(), nullable=False),
        sa.Column('image_path', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    _index('ix_shelf_snapshots_shelf_id', 'shelf_snapshots', ['shelf_id'], unique=False)
    _table('shelves',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('store_id', sa.String(), nullable=False),
        sa.Column('shelf_id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    _index('ix_shelves_shelf_id', 'shelves', ['shelf_id'], unique=True)
    _index('ix_shelves_store_id', 'shelves', ['store_id'], unique=False)
    _table('user_security',
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('session_epoch', sa.Integer(), nullable=False),
        sa.Column('totp_secret_enc', sa.String(), nullable=True),
        sa.Column('totp_pending_enc', sa.String(), nullable=True),
        sa.Column('totp_enabled_at', sa.DateTime(), nullable=True),
        sa.Column('totp_last_step', sa.BigInteger(), nullable=True),
        sa.Column('is_owner', sa.Boolean(), nullable=False),
        sa.Column('must_change_password', sa.Boolean(), nullable=False),
        sa.Column('temp_password_expires_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id')
    )
    _table('user_store_access',
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('store_id', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'store_id')
    )
    _table('auth_session_meta',
        sa.Column('token_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('session_epoch', sa.Integer(), nullable=False),
        sa.Column('device_label', sa.String(), nullable=True),
        sa.Column('device_kind', sa.String(), nullable=False),
        sa.Column('device_os', sa.String(), nullable=False),
        sa.Column('device_browser', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('ip_hash', sa.String(), nullable=True),
        sa.Column('mfa', sa.Boolean(), nullable=False),
        sa.Column('scope', sa.String(), nullable=False),
        sa.Column('listener', sa.String(), nullable=False),
        sa.Column('auth_at', sa.DateTime(), nullable=True),
        sa.Column('reauth_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['token_id'], ['api_tokens.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('token_id')
    )
    _index('ix_auth_session_meta_user_id', 'auth_session_meta', ['user_id'], unique=False)
    _table('camera_events',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('camera_id', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('snapshot_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    _index('ix_camera_events_camera_id', 'camera_events', ['camera_id'], unique=False)
    _index('ix_camera_events_created_at', 'camera_events', ['created_at'], unique=False)
    _table('compliance_reports',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('snapshot_id', sa.String(), nullable=False),
        sa.Column('compliance_score', sa.Float(), nullable=False),
        sa.Column('osa_score', sa.Float(), nullable=False),
        sa.Column('violations_json', sa.Text(), nullable=False),
        sa.Column('empty_slots_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['snapshot_id'], ['shelf_snapshots.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('snapshot_id')
    )
    _table('detections',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('snapshot_id', sa.String(), nullable=False),
        sa.Column('x1', sa.Float(), nullable=False),
        sa.Column('y1', sa.Float(), nullable=False),
        sa.Column('x2', sa.Float(), nullable=False),
        sa.Column('y2', sa.Float(), nullable=False),
        sa.Column('detector_confidence', sa.Float(), nullable=False),
        sa.Column('matched_product_id', sa.String(), nullable=True),
        sa.Column('match_score', sa.Float(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('shelf_row', sa.Integer(), nullable=True),
        sa.Column('slot_index', sa.Integer(), nullable=True),
        sa.Column('price_status', sa.String(), nullable=True),
        sa.Column('detected_price', sa.Float(), nullable=True),
        sa.Column('price_diff_pct', sa.Float(), nullable=True),
        sa.Column('reviewed_by', sa.String(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('review_action', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ),
        sa.ForeignKeyConstraint(['snapshot_id'], ['shelf_snapshots.id'], ),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    raise RuntimeError("Откат ниже базовой ревизии запрещён: он удалил бы все таблицы с данными. Восстановление — из резервной копии.")
