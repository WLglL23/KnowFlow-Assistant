"""Remove the circular summary cursor foreign key.

Revision ID: 20260809_0004
Revises: 20260809_0003
Create Date: 2026-08-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260809_0004"
down_revision: str | None = "20260809_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_conversations_summary_through_message_id",
        "conversations",
        type_="foreignkey",
    )


def downgrade() -> None:
    op.create_foreign_key(
        "fk_conversations_summary_through_message_id",
        "conversations",
        "messages",
        ["summary_through_message_id"],
        ["id"],
    )
