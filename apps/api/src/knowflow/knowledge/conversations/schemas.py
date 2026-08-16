from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

from knowflow.knowledge.answering.models import Message, MessageRole, MessageStatus
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


class MessageResponse(BaseModel):
    id: str
    role: MessageRole
    content: str
    status: MessageStatus
    citations: list[dict[str, object]]
    created_at: datetime

    @classmethod
    def from_model(cls, message: Message) -> MessageResponse:
        return cls(
            id=message.id,
            role=message.role,
            content=message.content,
            status=message.status,
            citations=message.citations_json,
            created_at=message.created_at,
        )

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC_PLUS_EIGHT).isoformat(timespec="microseconds")


class ConversationDetailResponse(ConversationResponse):
    messages: list[MessageResponse]


class ConversationPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    knowledge_mode: KnowledgeMode | None = None
    selected_source_ids: list[str] | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not 1 <= len(normalized) <= 100:
            raise ValueError("title must contain 1 to 100 characters")
        return normalized

    @model_validator(mode="after")
    def validate_patch(self) -> ConversationPatchRequest:
        fields = self.model_fields_set
        if not fields:
            raise ValueError("at least one field is required")
        if "title" in fields and self.title is None:
            raise ValueError("title cannot be null")
        scope_fields = {"knowledge_mode", "selected_source_ids"}
        if fields & scope_fields and not scope_fields <= fields:
            raise ValueError("knowledge scope fields must be provided together")
        if scope_fields <= fields:
            if self.knowledge_mode is None or self.selected_source_ids is None:
                raise ValueError("knowledge scope fields cannot be null")
            if len(self.selected_source_ids) != len(set(self.selected_source_ids)):
                raise ValueError("selected source ids must be unique")
            if (
                self.knowledge_mode in {KnowledgeMode.DISABLED, KnowledgeMode.ALL}
                and self.selected_source_ids
            ):
                raise ValueError("disabled and all modes require an empty selection")
        return self
