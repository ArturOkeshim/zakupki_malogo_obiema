"""add price research fields

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "specification_items",
        sa.Column(
            "price_research_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "specification_items",
        sa.Column("market_price_per_unit", sa.Numeric(precision=15, scale=2), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("market_confidence", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("market_item_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("market_note", sa.Text(), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("market_sources_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("market_analyzed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "auctions",
        sa.Column("margin_rub", sa.Numeric(precision=15, scale=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("auctions", "margin_rub")
    op.drop_column("specification_items", "market_analyzed_at")
    op.drop_column("specification_items", "market_sources_json")
    op.drop_column("specification_items", "market_note")
    op.drop_column("specification_items", "market_item_type")
    op.drop_column("specification_items", "market_confidence")
    op.drop_column("specification_items", "market_price_per_unit")
    op.drop_column("specification_items", "price_research_status")
