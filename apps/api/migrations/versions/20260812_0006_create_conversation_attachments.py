"""Create persistent conversation attachment queue.

Revision ID: 20260812_0006
Revises: 20260809_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260812_0006"
down_revision: str | None = "20260809_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_attachments",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(37), nullable=False),
        sa.Column("owner_account_id", sa.String(36), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("status", mysql.ENUM("QUEUED", "PROCESSING", "READY", "FAILED"), nullable=False),
        sa.Column(
            "processing_stage",
            mysql.ENUM("PARSING", "CHUNKING", "EMBEDDING", "INDEXING"),
            nullable=True,
        ),
        sa.Column("processing_progress", sa.Integer(), nullable=True),
        sa.Column("processing_error_code", sa.String(64), nullable=True),
        sa.Column("queued_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("processing_started_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("processed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["owner_account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "content_sha256", name="uq_attachment_conversation_sha"
        ),
    )
    op.create_index(
        "ix_attachment_queue", "conversation_attachments", ["status", "created_at", "id"]
    )
    op.create_index(
        "ix_attachment_conversation",
        "conversation_attachments",
        ["conversation_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_attachment_conversation", table_name="conversation_attachments")
    op.drop_index("ix_attachment_queue", table_name="conversation_attachments")
    op.drop_table("conversation_attachments")
