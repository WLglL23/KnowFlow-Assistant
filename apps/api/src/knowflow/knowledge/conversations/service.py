from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.errors import AppError
from knowflow.knowledge.answering.models import Message
from knowflow.knowledge.conversations.models import Conversation, KnowledgeMode
from knowflow.knowledge.conversations.schemas import (
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationResponse,
    MessageResponse,
)
from knowflow.knowledge.documents.models import (
    AttachmentStatus,
    ConversationAttachment,
    KnowledgeSubmission,
    KnowledgeSubmissionStatus,
)

OCCUPYING_ATTACHMENT_STATUSES = (
    AttachmentStatus.QUEUED,
    AttachmentStatus.PROCESSING,
    AttachmentStatus.READY,
)


class ConversationService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(self, owner_account_id: str) -> ConversationResponse:
        now = self._utc_now()
        conversation = Conversation(
            id=f"conv_{uuid.uuid4().hex}",
            owner_account_id=owner_account_id,
            title="新会话",
            knowledge_mode=KnowledgeMode.DISABLED,
            selected_source_ids_json=[],
            created_at=now,
            updated_at=now,
        )
        async with self._sessions() as session, session.begin():
            session.add(conversation)
        return ConversationResponse.from_model(conversation)

    async def list_for_owner(self, owner_account_id: str) -> ConversationListResponse:
        async with self._sessions() as session:
            conversations = (
                await session.scalars(
                    select(Conversation)
                    .where(Conversation.owner_account_id == owner_account_id)
                    .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                )
            ).all()
        return ConversationListResponse(
            items=[ConversationResponse.from_model(item) for item in conversations]
        )

    async def get_detail(
        self, conversation_id: str, owner_account_id: str
    ) -> ConversationDetailResponse:
        async with self._sessions() as session:
            conversation = await self._visible_conversation(
                session, conversation_id, owner_account_id
            )
            messages = list(
                (
                    await session.scalars(
                        select(Message)
                        .where(Message.conversation_id == conversation_id)
                        .order_by(Message.created_at.asc(), Message.id.asc())
                    )
                ).all()
            )
        response = ConversationResponse.from_model(conversation)
        return ConversationDetailResponse(
            **response.model_dump(),
            messages=[MessageResponse.from_model(message) for message in messages],
        )

    async def rename(
        self, conversation_id: str, owner_account_id: str, title: str
    ) -> ConversationResponse:
        return await self.update(
            conversation_id,
            owner_account_id,
            title=title,
            knowledge_mode=None,
            selected_source_ids=None,
        )

    async def update(
        self,
        conversation_id: str,
        owner_account_id: str,
        *,
        title: str | None,
        knowledge_mode: KnowledgeMode | None,
        selected_source_ids: list[str] | None,
    ) -> ConversationResponse:
        async with self._sessions() as session, session.begin():
            conversation = await self._visible_conversation(
                session, conversation_id, owner_account_id, for_update=True
            )
            normalized_selection: list[str] | None = None
            if knowledge_mode is not None and selected_source_ids is not None:
                normalized_selection = list(dict.fromkeys(selected_source_ids))
                if knowledge_mode == KnowledgeMode.SELECTED:
                    await self.validate_selected_sources(session, normalized_selection)
                occupying = await session.scalar(
                    select(func.count())
                    .select_from(ConversationAttachment)
                    .where(
                        ConversationAttachment.conversation_id == conversation_id,
                        ConversationAttachment.status.in_(OCCUPYING_ATTACHMENT_STATUSES),
                    )
                )
                selected_count = (
                    len(normalized_selection)
                    if knowledge_mode == KnowledgeMode.SELECTED
                    else 0
                )
                if (occupying or 0) + selected_count > 5:
                    raise AppError(409, "SOURCE_LIMIT_EXCEEDED", "来源数量已达到上限")

            changed = False
            if title is not None and conversation.title != title:
                conversation.title = title
                changed = True
            if knowledge_mode is not None and normalized_selection is not None:
                if (
                    conversation.knowledge_mode != knowledge_mode
                    or conversation.selected_source_ids_json != normalized_selection
                ):
                    conversation.knowledge_mode = knowledge_mode
                    conversation.selected_source_ids_json = normalized_selection
                    changed = True
            if changed:
                conversation.updated_at = max(
                    self._utc_now(), conversation.updated_at + timedelta(microseconds=1)
                )
        return ConversationResponse.from_model(conversation)

    @staticmethod
    async def validate_selected_sources(
        session: AsyncSession, selected_source_ids: list[str]
    ) -> None:
        if not selected_source_ids:
            return
        valid_ids = set(
            (
                await session.scalars(
                    select(KnowledgeSubmission.id).where(
                        KnowledgeSubmission.id.in_(selected_source_ids),
                        KnowledgeSubmission.status == KnowledgeSubmissionStatus.PUBLISHED,
                        KnowledgeSubmission.enabled.is_(True),
                    ).with_for_update()
                )
            ).all()
        )
        if valid_ids != set(selected_source_ids):
            raise AppError(409, "SOURCE_NOT_AVAILABLE", "知识来源当前不可用")

    async def _visible_conversation(
        self,
        session: AsyncSession,
        conversation_id: str,
        owner_account_id: str,
        *,
        for_update: bool = False,
    ) -> Conversation:
        statement = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.owner_account_id == owner_account_id,
        )
        if for_update:
            statement = statement.with_for_update()
        conversation = await session.scalar(statement)
        if conversation is None:
            raise AppError(404, "NOT_FOUND", "资源不存在")
        return conversation

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)


async def allowed_source_scope(
    session: AsyncSession, conversation: Conversation
) -> dict[str, object]:
    attachments = list(
        (
            await session.scalars(
                select(ConversationAttachment)
                .where(
                    ConversationAttachment.conversation_id == conversation.id,
                    ConversationAttachment.owner_account_id == conversation.owner_account_id,
                    ConversationAttachment.status == AttachmentStatus.READY,
                )
                .order_by(ConversationAttachment.created_at, ConversationAttachment.id)
            )
        ).all()
    )
    attachment_hashes = {item.content_sha256 for item in attachments}

    sources: list[KnowledgeSubmission] = []
    if conversation.knowledge_mode == KnowledgeMode.SELECTED:
        selected_ids = list(conversation.selected_source_ids_json)
        await ConversationService.validate_selected_sources(session, selected_ids)
        if selected_ids:
            by_id = {
                item.id: item
                for item in (
                    await session.scalars(
                        select(KnowledgeSubmission).where(
                            KnowledgeSubmission.id.in_(selected_ids)
                        ).with_for_update()
                    )
                ).all()
            }
            sources = [by_id[source_id] for source_id in selected_ids]
    elif conversation.knowledge_mode == KnowledgeMode.ALL:
        sources = list(
            (
                await session.scalars(
                    select(KnowledgeSubmission)
                    .where(
                        KnowledgeSubmission.status == KnowledgeSubmissionStatus.PUBLISHED,
                        KnowledgeSubmission.enabled.is_(True),
                    )
                    .order_by(KnowledgeSubmission.created_at, KnowledgeSubmission.id)
                    .with_for_update()
                )
            ).all()
        )

    return {
        "attachment_ids": [item.id for item in attachments],
        "knowledge_source_ids": [
            item.id for item in sources if item.content_sha256 not in attachment_hashes
        ],
        "knowledge_mode": conversation.knowledge_mode.value,
    }
