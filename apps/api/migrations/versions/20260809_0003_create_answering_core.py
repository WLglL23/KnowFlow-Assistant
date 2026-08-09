"""Create the minimal answering persistence core.

Revision ID: 20260809_0003
Revises: 20260803_0002
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260809_0003"
down_revision: str | None = "20260803_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("summary_text", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("summary_through_message_id", sa.String(length=36), nullable=True),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=37), nullable=False),
        sa.Column("role", mysql.ENUM("USER", "ASSISTANT"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "status", mysql.ENUM("PENDING", "COMPLETE", "PARTIAL"), nullable=False
        ),
        sa.Column("citations_json", mysql.JSON(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_foreign_key(
        "fk_conversations_summary_through_message_id",
        "conversations",
        "messages",
        ["summary_through_message_id"],
        ["id"],
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_account_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=37), nullable=False),
        sa.Column("user_message_id", sa.String(length=36), nullable=False),
        sa.Column("assistant_message_id", sa.String(length=36), nullable=False),
        sa.Column("client_request_id", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            mysql.ENUM("RUNNING", "SUCCEEDED", "FAILED", "PARTIAL"),
            nullable=False,
        ),
        sa.Column("route", mysql.ENUM("GENERAL", "RETRIEVAL"), nullable=True),
        sa.Column("source_scope_json", mysql.JSON(), nullable=False),
        sa.Column("trace_version", sa.String(length=64), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("trace_json", mysql.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.ForeignKeyConstraint(["actor_account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["assistant_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["user_message_id"], ["messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agent_run_stages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column(
            "stage",
            mysql.ENUM(
                "UNDERSTAND", "PLAN", "RETRIEVE_1", "VERIFY", "RETRIEVE_2", "GENERATE"
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            mysql.ENUM("RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"),
            nullable=False,
        ),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("summary_json", mysql.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("agent_run_stages")
    op.drop_table("agent_runs")
    op.drop_constraint(
        "fk_conversations_summary_through_message_id",
        "conversations",
        type_="foreignkey",
    )
    op.drop_table("messages")
    op.drop_column("conversations", "summary_through_message_id")
    op.drop_column("conversations", "summary_text")
