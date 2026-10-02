"""add yandex price research fields

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-14 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "specification_items",
        sa.Column(
            "yandex_research_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "specification_items",
        sa.Column("yandex_price_min", sa.Numeric(precision=15, scale=2), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("yandex_price_max", sa.Numeric(precision=15, scale=2), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("yandex_price_median", sa.Numeric(precision=15, scale=2), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("yandex_tender_vs_market", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("yandex_cards_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("yandex_note", sa.Text(), nullable=True),
    )
    op.add_column(
        "specification_items",
        sa.Column("yandex_analyzed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "auctions",
        sa.Column("fit_status_yandex", sa.String(length=20), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "auctions",
        sa.Column("margin_rub_yandex", sa.Numeric(precision=15, scale=2), nullable=True),
    )
    op.add_column(
        "auctions",
        sa.Column("margin_percent_yandex", sa.Numeric(precision=8, scale=2), nullable=True),
    )
    op.add_column(
        "auctions",
        sa.Column("rating_yandex", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("auctions", "rating_yandex")
    op.drop_column("auctions", "margin_percent_yandex")
    op.drop_column("auctions", "margin_rub_yandex")
    op.drop_column("auctions", "fit_status_yandex")
    op.drop_column("specification_items", "yandex_analyzed_at")
    op.drop_column("specification_items", "yandex_note")
    op.drop_column("specification_items", "yandex_cards_json")
    op.drop_column("specification_items", "yandex_tender_vs_market")
    op.drop_column("specification_items", "yandex_price_median")
    op.drop_column("specification_items", "yandex_price_max")
    op.drop_column("specification_items", "yandex_price_min")
    op.drop_column("specification_items", "yandex_research_status")
