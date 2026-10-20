"""add market retry and search api fields

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-20 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "specification_items",
        sa.Column("market_browser_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "specification_items",
        sa.Column("market_fail_reason", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("market_api_used", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("specification_items", "market_api_used")
    op.drop_column("specification_items", "market_fail_reason")
    op.drop_column("specification_items", "market_browser_attempts")
