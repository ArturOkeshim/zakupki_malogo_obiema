"""add sku card fields

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-05 16:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("specification_items", sa.Column("sku_model", sa.Text(), nullable=True))
    op.add_column("specification_items", sa.Column("sku_manufacturer", sa.Text(), nullable=True))
    op.add_column(
        "specification_items",
        sa.Column("sku_characteristics_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("sku_scraped_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("specification_items", "sku_scraped_at")
    op.drop_column("specification_items", "sku_characteristics_json")
    op.drop_column("specification_items", "sku_manufacturer")
    op.drop_column("specification_items", "sku_model")
