from __future__ import annotations

import io
from typing import cast

from pypdf import PdfReader, PdfWriter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.errors import AppError
from knowflow.knowledge.answering.models import Message, MessageRole
from knowflow.knowledge.conversations.models import Conversation
from knowflow.knowledge.documents.attachments import AttachmentDocuments
from knowflow.knowledge.documents.models import ConversationAttachment


class CitationPreview:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        documents: AttachmentDocuments,
    ) -> None:
        self._sessions = sessions
        self._documents = documents

    async def render(
        self, message_id: str, citation_index: int, actor_id: str
    ) -> tuple[bytes, str]:
        if citation_index < 0:
            raise AppError(404, "NOT_FOUND", "资源不存在")
        async with self._sessions() as session:
            row = await session.execute(
                select(Message, Conversation)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Message.id == message_id,
                    Message.role == MessageRole.ASSISTANT,
                    Conversation.owner_account_id == actor_id,
                )
            )
            result = row.first()
            if result is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            message, conversation = result
            if citation_index >= len(message.citations_json):
                raise AppError(404, "NOT_FOUND", "资源不存在")
            citation = message.citations_json[citation_index]
            source_id = citation.get("source_id")
            pages = citation.get("pages")
            if not isinstance(source_id, str) or not isinstance(pages, list):
                raise AppError(404, "NOT_FOUND", "资源不存在")
            page_numbers = cast(list[object], pages)
            attachment = await session.scalar(
                select(ConversationAttachment).where(
                    ConversationAttachment.id == source_id,
                    ConversationAttachment.conversation_id == conversation.id,
                    ConversationAttachment.owner_account_id == actor_id,
                    ConversationAttachment.deleted_at.is_(None),
                )
            )
            if attachment is None:
                tombstone = await session.scalar(
                    select(ConversationAttachment.id).where(
                        ConversationAttachment.id == source_id,
                        ConversationAttachment.conversation_id == conversation.id,
                        ConversationAttachment.owner_account_id == actor_id,
                        ConversationAttachment.deleted_at.is_not(None),
                    )
                )
                if tombstone is not None:
                    raise AppError(410, "CITATION_SOURCE_DELETED", "引用来源已删除")
                raise AppError(404, "NOT_FOUND", "资源不存在")
            object_key = attachment.object_key
            filename = attachment.filename
        try:
            content = await self._documents.read_source_object(object_key)
            reader = PdfReader(io.BytesIO(content))
            writer = PdfWriter()
            for page_number in page_numbers[:5]:
                if not isinstance(page_number, int) or not 1 <= page_number <= len(reader.pages):
                    raise AppError(404, "NOT_FOUND", "资源不存在")
                writer.add_page(reader.pages[page_number - 1])
            output = io.BytesIO()
            writer.write(output)
            return output.getvalue(), filename
        except AppError:
            raise
        except Exception as exc:
            raise AppError(500, "PREVIEW_FAILED", "引用页预览失败") from exc
