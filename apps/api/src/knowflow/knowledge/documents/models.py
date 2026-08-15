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


class KnowledgeSubmissionStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    REJECTED = "REJECTED"


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


class KnowledgeSubmission(Base):
    __tablename__ = "knowledge_submissions"
    __table_args__ = (
        UniqueConstraint("active_content_sha256", name="uq_submission_active_content_sha"),
        Index("ix_submission_owner_created", "submitter_account_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    submitter_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("accounts.id"))
    reviewer_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("accounts.id"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(String(512))
    content_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[KnowledgeSubmissionStatus] = mapped_column(
        Enum(KnowledgeSubmissionStatus, native_enum=True)
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    processing_stage: Mapped[ProcessingStage | None] = mapped_column(
        Enum(ProcessingStage, native_enum=True), nullable=True
    )
    processing_progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
