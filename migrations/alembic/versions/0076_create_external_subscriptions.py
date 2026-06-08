"""create external subscription sources and configs

Revision ID: 0076
Revises: 0075
Create Date: 2026-06-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0076'
down_revision: Union[str, None] = '0075'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'external_subscription_sources',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('headers', sa.JSON(), nullable=True),
        sa.Column('refresh_interval_minutes', sa.Integer(), nullable=False, server_default='360'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('last_fetched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_status', sa.String(length=20), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('configs_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        'ix_external_subscription_sources_id', 'external_subscription_sources', ['id']
    )

    op.create_table(
        'external_configs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'source_id',
            sa.Integer(),
            sa.ForeignKey('external_subscription_sources.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('raw_link', sa.Text(), nullable=False),
        sa.Column('protocol', sa.String(length=20), nullable=True),
        sa.Column('remote_key', sa.String(length=255), nullable=False),
        sa.Column('is_selected', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('source_id', 'remote_key', name='uq_external_config_source_remote'),
    )
    op.create_index('ix_external_configs_id', 'external_configs', ['id'])
    op.create_index('ix_external_configs_source_id', 'external_configs', ['source_id'])
    op.create_index(
        'ix_external_configs_selected_active', 'external_configs', ['is_selected', 'is_active']
    )


def downgrade() -> None:
    op.drop_index('ix_external_configs_selected_active', table_name='external_configs')
    op.drop_index('ix_external_configs_source_id', table_name='external_configs')
    op.drop_index('ix_external_configs_id', table_name='external_configs')
    op.drop_table('external_configs')
    op.drop_index('ix_external_subscription_sources_id', table_name='external_subscription_sources')
    op.drop_table('external_subscription_sources')
