from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from knowflow.knowledge.documents.models import ConversationAttachment


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
