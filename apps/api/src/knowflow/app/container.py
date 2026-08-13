"""M01 dependency container."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from knowflow.app.config import Settings
from knowflow.identity.service import IdentityService
from knowflow.knowledge.answering.service import AnsweringService
from knowflow.knowledge.conversations.service import ConversationService
from knowflow.knowledge.documents.attachments import AttachmentDocuments


@dataclass(frozen=True, slots=True)
class AppContainer:
    """M01 dependencies assembled without eagerly opening a database connection."""

    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    identity: IdentityService
    conversations: ConversationService
    answering: AnsweringService
    documents: AttachmentDocuments
