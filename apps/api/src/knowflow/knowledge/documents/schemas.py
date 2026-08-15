from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from knowflow.knowledge.documents.models import ConversationAttachment, KnowledgeSubmission


class AttachmentResponse(BaseModel):
    id: str
    filename: str
    size_bytes: int
    page_count: int
    status: str
    processing_stage: str | None
    processing_progress: int | None
    processing_error_code: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, value: ConversationAttachment) -> AttachmentResponse:
        return cls.model_validate({name: getattr(value, name) for name in cls.model_fields})


class AttachmentUploadResponse(BaseModel):
    result: Literal[
        "CREATED", "EXISTING_QUEUED", "EXISTING_PROCESSING", "ALREADY_EXISTS", "RETRY_QUEUED"
    ]
    attachment: AttachmentResponse


class AttachmentListResponse(BaseModel):
    items: list[AttachmentResponse]


class SubmissionResponse(BaseModel):
    id: str
    filename: str
    size_bytes: int
    page_count: int
    status: str
    rejection_reason: str | None
    processing_stage: str | None
    processing_progress: int | None
    processing_error_code: str | None
    created_at: datetime
    reviewed_at: datetime | None
    published_at: datetime | None

    @classmethod
    def from_model(cls, value: KnowledgeSubmission) -> SubmissionResponse:
        return cls.model_validate({name: getattr(value, name) for name in cls.model_fields})


class SubmissionUploadResponse(BaseModel):
    result: Literal["CREATED", "ALREADY_ACTIVE"]
    submission: SubmissionResponse | None = None


class SubmissionListResponse(BaseModel):
    items: list[SubmissionResponse]
