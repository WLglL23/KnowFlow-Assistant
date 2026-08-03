"""Create the conversations table.

Revision ID: 20260803_0002
Revises: 20260801_0001
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260803_0002"
down_revision: str | None = "20260801_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=37), nullable=False),
        sa.Column("owner_account_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column(
            "knowledge_mode",
            mysql.ENUM("DISABLED", "ALL", "SELECTED"),
            nullable=False,
        ),
        sa.Column("selected_source_ids_json", mysql.JSON(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["owner_account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversations_owner_updated_id",
        "conversations",
        ["owner_account_id", "updated_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_owner_updated_id", table_name="conversations")
    op.drop_table("conversations")
