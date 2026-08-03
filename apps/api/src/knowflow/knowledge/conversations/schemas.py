from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from knowflow.knowledge.conversations.models import Conversation, KnowledgeMode

UTC_PLUS_EIGHT = timezone(timedelta(hours=8))


class ConversationResponse(BaseModel):
    id: str
    title: str
    knowledge_mode: KnowledgeMode
    selected_source_ids: list[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, conversation: Conversation) -> ConversationResponse:
        return cls(
            id=conversation.id,
            title=conversation.title,
            knowledge_mode=conversation.knowledge_mode,
            selected_source_ids=conversation.selected_source_ids_json,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC_PLUS_EIGHT).isoformat(timespec="microseconds")


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]


class ConversationDetailResponse(ConversationResponse):
    messages: list[dict[str, object]]


class ConversationPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not 1 <= len(normalized) <= 100:
            raise ValueError("title must contain 1 to 100 characters")
        return normalized
