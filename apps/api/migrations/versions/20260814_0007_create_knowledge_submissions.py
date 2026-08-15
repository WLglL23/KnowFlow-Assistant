"""Create the M04 knowledge submission record.

Revision ID: 20260814_0007
Revises: 20260812_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260814_0007"
down_revision: str | None = "20260812_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_submissions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("submitter_account_id", sa.String(36), nullable=False),
        sa.Column("reviewer_account_id", sa.String(36), nullable=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            mysql.ENUM(
                "PENDING_REVIEW",
                "PUBLISHING",
                "PUBLISHED",
                "PUBLISH_FAILED",
                "REJECTED",
            ),
            nullable=False,
        ),
        sa.Column("rejection_reason", sa.String(500), nullable=True),
        sa.Column(
            "processing_stage",
            mysql.ENUM("PARSING", "CHUNKING", "EMBEDDING", "INDEXING"),
            nullable=True,
        ),
        sa.Column("processing_progress", sa.Integer(), nullable=True),
        sa.Column("processing_error_code", sa.String(64), nullable=True),
        sa.Column("active_content_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("reviewed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("published_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.ForeignKeyConstraint(["reviewer_account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["submitter_account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_content_sha256", name="uq_submission_active_content_sha"),
    )
    op.create_index(
        "ix_submission_owner_created",
        "knowledge_submissions",
        ["submitter_account_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_submission_owner_created", table_name="knowledge_submissions")
    op.drop_table("knowledge_submissions")
