"""add price search run analytics

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-20 15:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g7h8i9j0k1l2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "price_search_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("auction_numbers_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("search_duration_sec", sa.Float(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("priced_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("not_found_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "price_search_item_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("specification_item_id", sa.Integer(), nullable=False),
        sa.Column("auction_number", sa.String(length=50), nullable=False),
        sa.Column("item_name", sa.Text(), nullable=True),
        sa.Column("search_stage", sa.String(length=20), nullable=False),
        sa.Column("item_kind", sa.String(length=20), nullable=False),
        sa.Column("search_channel", sa.String(length=20), nullable=False),
        sa.Column("worker_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("fail_reason", sa.String(length=30), nullable=True),
        sa.Column("error_type", sa.String(length=20), nullable=True),
        sa.Column("found_on_attempt", sa.Integer(), nullable=True),
        sa.Column("json_repaired", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("http_403", sa.Boolean(), nullable=False, server_default="false"),
        sa.ForeignKeyConstraint(["run_id"], ["price_search_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_price_search_item_events_run_id",
        "price_search_item_events",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_price_search_item_events_run_id", table_name="price_search_item_events")
    op.drop_table("price_search_item_events")
    op.drop_table("price_search_runs")
