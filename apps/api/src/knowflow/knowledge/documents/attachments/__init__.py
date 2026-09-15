from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.config import Settings
from knowflow.app.errors import AppError
from knowflow.knowledge.conversations.models import Conversation, KnowledgeMode
from knowflow.knowledge.documents.chunks import DocumentChunk, chunk_document
from knowflow.knowledge.documents.models import (
    AttachmentStatus,
    CleanupStatus,
    ConversationAttachment,
    ProcessingStage,
)
from knowflow.knowledge.documents.object_store import PrivateObjectStore
from knowflow.knowledge.documents.pdf import PdfDocuments
from knowflow.knowledge.documents.schemas import (
    AttachmentListResponse,
    AttachmentResponse,
    AttachmentUploadResponse,
)

logger = logging.getLogger("knowflow.documents.attachments")


class AttachmentDocuments:
    """Conversation attachment capability exposed by the composition root."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        pdf: PdfDocuments,
        objects: PrivateObjectStore,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._pdf = pdf
        self._objects = objects
        self._workers: list[asyncio.Task[None]] = []
        self._wake = asyncio.Event()
        self._stopping = False

    async def start(self) -> None:
        if self._workers:
            return
        self._stopping = False
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(ConversationAttachment)
                .where(ConversationAttachment.status == AttachmentStatus.PROCESSING)
                .values(
                    status=AttachmentStatus.QUEUED,
                    processing_stage=None,
                    processing_progress=None,
                    processing_error_code=None,
                    queued_at=datetime.now(UTC).replace(tzinfo=None),
                    processing_started_at=None,
                    processed_at=None,
                )
            )
            queued = await session.scalar(
                select(func.count())
                .select_from(ConversationAttachment)
                .where(ConversationAttachment.status == AttachmentStatus.QUEUED)
            )
        self._workers = [asyncio.create_task(self._worker()) for _ in range(2)]
        if queued:
            self._wake.set()

    async def stop(self) -> None:
        if not self._workers:
            return
        self._stopping = True
        self._wake.set()
        await asyncio.gather(*self._workers)
        self._workers.clear()

    async def dispatch(self) -> None:
        self._wake.set()

    async def upload(
        self,
        conversation_id: str,
        owner_account_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> tuple[int, AttachmentUploadResponse]:
        page_count = await self._pdf.validate(
            filename,
            content_type,
            content,
            max_bytes=self._settings.attachment_max_bytes,
        )
        digest = hashlib.sha256(content).hexdigest()
        now = datetime.now(UTC).replace(tzinfo=None)
        attachment: ConversationAttachment | None = None
        result = "CREATED"
        status_code = 201
        try:
            async with self._sessions() as session, session.begin():
                conversation = await session.scalar(
                    select(Conversation)
                    .where(
                        Conversation.id == conversation_id,
                        Conversation.owner_account_id == owner_account_id,
                        Conversation.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if conversation is None:
                    raise AppError(404, "NOT_FOUND", "资源不存在")
                attachment = await session.scalar(
                    select(ConversationAttachment).where(
                        ConversationAttachment.conversation_id == conversation_id,
                        ConversationAttachment.content_sha256 == digest,
                        ConversationAttachment.deleted_at.is_(None),
                    )
                )
                selected_count = (
                    len(conversation.selected_source_ids_json)
                    if conversation.knowledge_mode == KnowledgeMode.SELECTED
                    else 0
                )
                if attachment is None or attachment.status == AttachmentStatus.FAILED:
                    active = await session.scalar(
                        select(func.count())
                        .select_from(ConversationAttachment)
                        .where(
                            ConversationAttachment.conversation_id == conversation_id,
                            ConversationAttachment.status.in_(
                                [
                                    AttachmentStatus.QUEUED,
                                    AttachmentStatus.PROCESSING,
                                    AttachmentStatus.READY,
                                ]
                            ),
                            ConversationAttachment.deleted_at.is_(None),
                        )
                    )
                    if (active or 0) + selected_count >= 5:
                        raise AppError(409, "SOURCE_LIMIT_EXCEEDED", "来源数量已达到上限")
                if attachment is not None:
                    result = {
                        AttachmentStatus.QUEUED: "EXISTING_QUEUED",
                        AttachmentStatus.PROCESSING: "EXISTING_PROCESSING",
                        AttachmentStatus.READY: "ALREADY_EXISTS",
                        AttachmentStatus.FAILED: "RETRY_QUEUED",
                    }[attachment.status]
                    status_code = 200
                    if attachment.status == AttachmentStatus.FAILED:
                        attachment.status = AttachmentStatus.QUEUED
                        attachment.processing_stage = None
                        attachment.processing_progress = None
                        attachment.processing_error_code = None
                        attachment.queued_at = now
                        attachment.processing_started_at = None
                        attachment.processed_at = None
                        attachment.updated_at = now
                else:
                    attachment_id = f"att_{uuid.uuid4().hex}"
                    attachment = ConversationAttachment(
                        id=attachment_id,
                        conversation_id=conversation_id,
                        owner_account_id=owner_account_id,
                        filename=filename,
                        size_bytes=len(content),
                        page_count=page_count,
                        content_sha256=digest,
                        object_key=f"attachments/{attachment_id}.pdf",
                        status=AttachmentStatus.QUEUED,
                        processing_stage=None,
                        processing_progress=None,
                        processing_error_code=None,
                        queued_at=now,
                        processing_started_at=None,
                        processed_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(attachment)
        except IntegrityError:
            async with self._sessions() as session:
                attachment = await session.scalar(
                    select(ConversationAttachment).where(
                        ConversationAttachment.conversation_id == conversation_id,
                        ConversationAttachment.deleted_at.is_(None),
                        ConversationAttachment.content_sha256 == digest,
                    )
                )
            if attachment is None:
                raise
            result, status_code = "EXISTING_QUEUED", 200
        assert attachment is not None
        response_attachment = AttachmentResponse.from_model(attachment)
        if result in {"CREATED", "RETRY_QUEUED"}:
            await self._objects.put(attachment.object_key, content)
            await self._ensure_index()
        return status_code, AttachmentUploadResponse(
            result=result,  # type: ignore[arg-type]
            attachment=response_attachment,
        )

    async def list_for_owner(
        self, conversation_id: str, owner_account_id: str
    ) -> AttachmentListResponse:
        async with self._sessions() as session:
            await self._visible_conversation(session, conversation_id, owner_account_id)
            items = (
                await session.scalars(
                    select(ConversationAttachment)
                    .where(
                        ConversationAttachment.conversation_id == conversation_id,
                        ConversationAttachment.deleted_at.is_(None),
                    )
                    .order_by(ConversationAttachment.created_at, ConversationAttachment.id)
                )
            ).all()
        return AttachmentListResponse(items=[AttachmentResponse.from_model(item) for item in items])

    async def detail(
        self, conversation_id: str, attachment_id: str, owner_account_id: str
    ) -> AttachmentResponse:
        async with self._sessions() as session:
            await self._visible_conversation(session, conversation_id, owner_account_id)
            attachment = await session.scalar(
                select(ConversationAttachment).where(
                    ConversationAttachment.id == attachment_id,
                    ConversationAttachment.conversation_id == conversation_id,
                    ConversationAttachment.deleted_at.is_(None),
                )
            )
            if attachment is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            return AttachmentResponse.from_model(attachment)

    async def delete(self, conversation_id: str, attachment_id: str, owner_account_id: str) -> None:
        async with self._sessions() as session, session.begin():
            await self._visible_conversation(session, conversation_id, owner_account_id)
            attachment = await session.scalar(
                select(ConversationAttachment)
                .where(
                    ConversationAttachment.id == attachment_id,
                    ConversationAttachment.conversation_id == conversation_id,
                )
                .with_for_update()
            )
            if attachment is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            if attachment.deleted_at is not None:
                return
            attachment.deleted_at = datetime.now(UTC).replace(tzinfo=None)
            attachment.es_cleanup_status = CleanupStatus.PENDING
            attachment.minio_cleanup_status = CleanupStatus.PENDING

    async def _visible_conversation(
        self, session: AsyncSession, conversation_id: str, owner: str
    ) -> None:
        if (
            await session.scalar(
                select(Conversation.id).where(
                    Conversation.id == conversation_id,
                    Conversation.owner_account_id == owner,
                    Conversation.deleted_at.is_(None),
                )
            )
            is None
        ):
            raise AppError(404, "NOT_FOUND", "资源不存在")

    async def _worker(self) -> None:
        while True:
            if self._stopping:
                return
            attachment_id = await self._claim()
            if attachment_id is None:
                if self._stopping:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            await self._process(attachment_id)

    async def _claim(self) -> str | None:
        async with self._sessions() as session, session.begin():
            item = await session.scalar(
                select(ConversationAttachment)
                .where(
                    ConversationAttachment.status == AttachmentStatus.QUEUED,
                    ConversationAttachment.processing_started_at.is_(None),
                    ConversationAttachment.deleted_at.is_(None),
                )
                .order_by(ConversationAttachment.queued_at, ConversationAttachment.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if item is None:
                return None
            item.status = AttachmentStatus.PROCESSING
            item.processing_stage = ProcessingStage.PARSING
            item.processing_progress = 10
            started_at = datetime.now(UTC).replace(tzinfo=None)
            item.processing_started_at = started_at
            item.updated_at = started_at
            return item.id

    async def _process(self, attachment_id: str) -> None:
        try:
            async with self._sessions() as session:
                item = await session.get(ConversationAttachment, attachment_id)
                if item is None or item.deleted_at is not None:
                    return
                content = await self._objects.get(item.object_key)
                digest = item.content_sha256
            document = await asyncio.to_thread(self._pdf.convert, item.filename, content)
            await self._progress(attachment_id, ProcessingStage.CHUNKING, 35)
            chunks = await asyncio.to_thread(self._chunk_document, document)
            await self._progress(attachment_id, ProcessingStage.EMBEDDING, 60)
            texts = [chunk.text for chunk in chunks]
            vectors = await self._embed(texts)
            await self._progress(attachment_id, ProcessingStage.INDEXING, 85)
            await self._replace_index(attachment_id, digest, chunks, vectors)
            await self._finish(attachment_id, True)
        except Exception:
            logger.exception(
                "Attachment processing failed",
                extra={"attachment_id": attachment_id},
            )
            await self._finish(attachment_id, False)

    async def _progress(self, attachment_id: str, stage: ProcessingStage, value: int) -> None:
        async with self._sessions() as session, session.begin():
            item = await session.get(ConversationAttachment, attachment_id)
            if item is not None and item.status == AttachmentStatus.PROCESSING:
                item.processing_stage, item.processing_progress = stage, value

    async def _finish(self, attachment_id: str, success: bool) -> None:
        async with self._sessions() as session, session.begin():
            item = await session.get(ConversationAttachment, attachment_id)
            if item is not None and item.status == AttachmentStatus.PROCESSING:
                item.status = AttachmentStatus.READY if success else AttachmentStatus.FAILED
                if success:
                    item.processing_stage = None
                    item.processing_progress = 100
                item.processing_error_code = None if success else "ATTACHMENT_PROCESSING_FAILED"
                item.processed_at = datetime.now(UTC).replace(tzinfo=None)

    @staticmethod
    def _chunk_document(document: object) -> list[DocumentChunk]:
        return chunk_document(document)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        if not self._settings.embedding_base_url:
            raise RuntimeError("embedding is not configured")
        headers: dict[str, str] = {}
        if self._settings.embedding_api_key:
            headers["Authorization"] = (
                f"Bearer {self._settings.embedding_api_key.get_secret_value()}"
            )
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=10) as client:
            for start in range(0, len(texts), 20):
                batch = texts[start : start + 20]
                response = await client.post(
                    f"{self._settings.embedding_base_url.rstrip('/')}/embeddings",
                    headers=headers,
                    json={"model": self._settings.embedding_model, "input": batch},
                )
                response.raise_for_status()
                vectors.extend(item["embedding"] for item in response.json()["data"])
        if len(vectors) != len(texts) or any(
            len(vector) != self._settings.embedding_dimensions for vector in vectors
        ):
            raise RuntimeError("embedding dimension mismatch")
        return vectors

    async def _replace_index(
        self,
        source_id: str,
        digest: str,
        chunks: list[DocumentChunk],
        vectors: list[list[float]],
    ) -> None:
        base, index = (
            self._settings.elasticsearch_url.rstrip("/"),
            self._settings.elasticsearch_index,
        )
        async with httpx.AsyncClient(timeout=10) as client:
            deleted = await client.post(
                f"{base}/{index}/_delete_by_query",
                params={"refresh": "true", "conflicts": "proceed"},
                json={"query": {"term": {"source_id": source_id}}},
            )
            deleted.raise_for_status()
            for chunk_number, (chunk, vector) in enumerate(
                zip(chunks, vectors), start=1
            ):
                chunk_id = f"{source_id}:{chunk_number}"
                indexed = await client.put(
                    f"{base}/{index}/_doc/{quote(chunk_id, safe='')}",
                    params={"refresh": "true"},
                    json={
                        "source_type": "ATTACHMENT",
                        "source_id": source_id,
                        "content_sha256": digest,
                        "chunk_id": chunk_id,
                        "page_numbers": chunk.pages,
                        "text": chunk.text,
                        "document_title": chunk.document_title,
                        "headings": chunk.headings,
                        "vector": vector,
                    },
                )
                indexed.raise_for_status()

    async def _ensure_index(self) -> None:
        base, index = (
            self._settings.elasticsearch_url.rstrip("/"),
            self._settings.elasticsearch_index,
        )
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.put(
                f"{base}/{index}",
                json={
                    "mappings": {
                        "properties": {
                            "source_id": {
                                "type": "text",
                                "fields": {"keyword": {"type": "keyword"}},
                            },
                            "vector": {
                                "type": "dense_vector",
                                "dims": self._settings.embedding_dimensions,
                                "index": False,
                            },
                        }
                    }
                },
            )
            if response.status_code not in {200, 400}:
                response.raise_for_status()

    async def read_source_object(self, key: str) -> bytes:
        return await self._objects.get(key)
