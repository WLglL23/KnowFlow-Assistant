"""Add the T024 publication and source management fields.

Revision ID: 20260815_0008
Revises: 20260814_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260815_0008"
down_revision: str | None = "20260814_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_submissions",
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "knowledge_submissions",
        sa.Column("disabled_at", mysql.DATETIME(fsp=6), nullable=True),
    )
    cleanup_status = mysql.ENUM("PENDING", "RUNNING", "SUCCEEDED", "FAILED")
    op.add_column(
        "knowledge_submissions",
        sa.Column("es_cleanup_status", cleanup_status, nullable=True),
    )
    op.add_column(
        "knowledge_submissions",
        sa.Column("es_cleanup_error_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "knowledge_submissions",
        sa.Column("es_cleanup_failed_at", mysql.DATETIME(fsp=6), nullable=True),
    )
    op.add_column(
        "knowledge_submissions",
        sa.Column(
            "minio_cleanup_status",
            mysql.ENUM("PENDING", "RUNNING", "SUCCEEDED", "FAILED"),
            nullable=True,
        ),
    )
    op.add_column(
        "knowledge_submissions",
        sa.Column("minio_cleanup_error_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "knowledge_submissions",
        sa.Column("minio_cleanup_failed_at", mysql.DATETIME(fsp=6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_submissions", "minio_cleanup_failed_at")
    op.drop_column("knowledge_submissions", "minio_cleanup_error_code")
    op.drop_column("knowledge_submissions", "minio_cleanup_status")
    op.drop_column("knowledge_submissions", "es_cleanup_failed_at")
    op.drop_column("knowledge_submissions", "es_cleanup_error_code")
    op.drop_column("knowledge_submissions", "es_cleanup_status")
    op.drop_column("knowledge_submissions", "disabled_at")
    op.drop_column("knowledge_submissions", "enabled")
