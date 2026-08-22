"""Add resource tombstones and document cleanup state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260822_0009"
down_revision: str | None = "20260815_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True))
    op.add_column(
        "conversation_attachments",
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
    )
    cleanup_status = mysql.ENUM("PENDING", "RUNNING", "SUCCEEDED", "FAILED")
    op.add_column("conversation_attachments", sa.Column("es_cleanup_status", cleanup_status))
    op.add_column(
        "conversation_attachments", sa.Column("es_cleanup_error_code", sa.String(64))
    )
    op.add_column(
        "conversation_attachments",
        sa.Column("es_cleanup_failed_at", mysql.DATETIME(fsp=6)),
    )
    op.add_column(
        "conversation_attachments",
        sa.Column("minio_cleanup_status", mysql.ENUM("PENDING", "RUNNING", "SUCCEEDED", "FAILED")),
    )
    op.add_column(
        "conversation_attachments", sa.Column("minio_cleanup_error_code", sa.String(64))
    )
    op.add_column(
        "conversation_attachments",
        sa.Column("minio_cleanup_failed_at", mysql.DATETIME(fsp=6)),
    )
    op.add_column(
        "knowledge_submissions",
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_submissions", "deleted_at")
    op.drop_column("conversation_attachments", "minio_cleanup_failed_at")
    op.drop_column("conversation_attachments", "minio_cleanup_error_code")
    op.drop_column("conversation_attachments", "minio_cleanup_status")
    op.drop_column("conversation_attachments", "es_cleanup_failed_at")
    op.drop_column("conversation_attachments", "es_cleanup_error_code")
    op.drop_column("conversation_attachments", "es_cleanup_status")
    op.drop_column("conversation_attachments", "deleted_at")
    op.drop_column("conversations", "deleted_at")
