from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from knowflow.app.database import Base


class AttachmentStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class ProcessingStage(StrEnum):
    PARSING = "PARSING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"


class ConversationAttachment(Base):
    __tablename__ = "conversation_attachments"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "content_sha256", name="uq_attachment_conversation_sha"
        ),
        Index("ix_attachment_queue", "status", "created_at", "id"),
        Index("ix_attachment_conversation", "conversation_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(37), ForeignKey("conversations.id"))
    owner_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"))
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[AttachmentStatus] = mapped_column(Enum(AttachmentStatus, native_enum=True))
    processing_stage: Mapped[ProcessingStage | None] = mapped_column(
        Enum(ProcessingStage, native_enum=True), nullable=True
    )
    processing_progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    processing_started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
