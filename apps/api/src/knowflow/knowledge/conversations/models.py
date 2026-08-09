from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from knowflow.app.database import Base


class KnowledgeMode(StrEnum):
    DISABLED = "DISABLED"
    ALL = "ALL"
    SELECTED = "SELECTED"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_owner_updated_id", "owner_account_id", "updated_at", "id"),
        Index("fk_conversations_summary_through_message_id", "summary_through_message_id"),
    )

    id: Mapped[str] = mapped_column(String(37), primary_key=True)
    owner_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    knowledge_mode: Mapped[KnowledgeMode] = mapped_column(
        Enum(KnowledgeMode, name="knowledge_mode", native_enum=True), nullable=False
    )
    selected_source_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_through_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
