"""external config display_name; tariff next_tariff_period_days + is_one_time; one-time purchases

Revision ID: 0077
Revises: 0076
Create Date: 2026-06-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0077'
down_revision: Union[str, None] = '0076'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Part 2: editable display name for external configs ---
    op.add_column('external_configs', sa.Column('display_name', sa.String(length=255), nullable=True))

    # --- Part 3: target period for auto-transition ---
    op.add_column('tariffs', sa.Column('next_tariff_period_days', sa.Integer(), nullable=True))
    # Backfill existing auto-transitions: default to the target tariff's minimal period,
    # so behaviour matches the previous get_shortest_period() logic.
    op.execute(
        """
        UPDATE tariffs t
        SET next_tariff_period_days = (
            SELECT MIN(k::int)
            FROM jsonb_object_keys((nt.period_prices)::jsonb) AS k
        )
        FROM tariffs nt
        WHERE t.next_tariff_id = nt.id
          AND t.next_tariff_id IS NOT NULL
          AND nt.period_prices IS NOT NULL
          AND (nt.period_prices)::jsonb <> '{}'::jsonb
        """
    )

    # --- Part 4: one-time tariff flag + purchase log ---
    op.add_column(
        'tariffs',
        sa.Column('is_one_time', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.create_table(
        'tariff_one_time_purchases',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'tariff_id', name='uq_one_time_purchase_user_tariff'),
    )
    op.create_index('ix_one_time_purchase_user', 'tariff_one_time_purchases', ['user_id'])
    op.create_index('ix_tariff_one_time_purchases_id', 'tariff_one_time_purchases', ['id'])


def downgrade() -> None:
    op.drop_index('ix_tariff_one_time_purchases_id', table_name='tariff_one_time_purchases')
    op.drop_index('ix_one_time_purchase_user', table_name='tariff_one_time_purchases')
    op.drop_table('tariff_one_time_purchases')
    op.drop_column('tariffs', 'is_one_time')
    op.drop_column('tariffs', 'next_tariff_period_days')
    op.drop_column('external_configs', 'display_name')
