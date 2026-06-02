"""add next_tariff_id to tariffs

Revision ID: 0075
Revises: 0074
Create Date: 2026-06-01

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0075'
down_revision: Union[str, None] = '0074'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'tariffs',
        sa.Column(
            'next_tariff_id',
            sa.Integer(),
            sa.ForeignKey('tariffs.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('tariffs', 'next_tariff_id')
