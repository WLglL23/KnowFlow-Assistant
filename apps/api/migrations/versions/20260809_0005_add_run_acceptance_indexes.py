"""Add indexes used by Run acceptance locking and conflict checks.

Revision ID: 20260809_0005
Revises: 20260809_0004
Create Date: 2026-08-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260809_0005"
down_revision: str | None = "20260809_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_runs_actor_status",
        "agent_runs",
        ["actor_account_id", "status"],
    )
    op.create_index(
        "ix_agent_runs_conversation_status",
        "agent_runs",
        ["conversation_id", "status"],
    )
    op.create_index(
        "ix_agent_runs_actor_conversation_request",
        "agent_runs",
        ["actor_account_id", "conversation_id", "client_request_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_actor_conversation_request", table_name="agent_runs")
    op.drop_index("ix_agent_runs_conversation_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_actor_status", table_name="agent_runs")
