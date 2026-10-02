"""add wb_offers table

Revision ID: a1b2c3d4e5f6
Revises: e9ef87cb3c6b
Create Date: 2026-09-01 21:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "e9ef87cb3c6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wb_offers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("specification_item_id", sa.Integer(), nullable=False),
        sa.Column("auction_number", sa.String(length=50), nullable=False),
        sa.Column("search_query", sa.Text(), nullable=False),
        sa.Column("result_rank", sa.Integer(), nullable=False),
        sa.Column("wb_name", sa.Text(), nullable=False),
        sa.Column("wb_price", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("wb_old_price", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("wb_url", sa.Text(), nullable=False),
        sa.Column("match_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("scraped_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["specification_item_id"],
            ["specification_items.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "specification_item_id",
            "result_rank",
            name="uq_wb_offer_spec_item_rank",
        ),
    )


def downgrade() -> None:
    op.drop_table("wb_offers")
