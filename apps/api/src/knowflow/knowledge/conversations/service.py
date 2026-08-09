from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
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
        async with self._sessions() as session, session.begin():
            conversation = await self._visible_conversation(
                session, conversation_id, owner_account_id, for_update=True
            )
            if conversation.title != title:
                conversation.title = title
                conversation.updated_at = max(
                    self._utc_now(), conversation.updated_at + timedelta(microseconds=1)
                )
        return ConversationResponse.from_model(conversation)

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
