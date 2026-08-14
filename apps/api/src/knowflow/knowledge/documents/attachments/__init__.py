from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import os
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import quote, urlsplit

import httpx
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.types.io import DocumentStream
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.config import Settings
from knowflow.app.errors import AppError
from knowflow.knowledge.conversations.models import Conversation
from knowflow.knowledge.documents.models import (
    AttachmentStatus,
    ConversationAttachment,
    ProcessingStage,
)
from knowflow.knowledge.documents.schemas import (
    AttachmentListResponse,
    AttachmentResponse,
    AttachmentUploadResponse,
)


class AttachmentDocuments:
    """Conversation attachment capability exposed by the composition root."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessions = sessions
        self._settings = settings
        self._workers: list[asyncio.Task[None]] = []
        self._wake = asyncio.Event()
        self._stopping = False
        self._converter_lock = threading.Lock()
        if settings.docling_hf_home:
            os.environ.setdefault("HF_HOME", settings.docling_hf_home)
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        options = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
        cast(Any, options.layout_options).engine_options.compile_model = False
        self._converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )

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
        page_count = await self._validate_pdf(filename, content_type, content)
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
                    )
                    .with_for_update()
                )
                if conversation is None:
                    raise AppError(404, "NOT_FOUND", "资源不存在")
                attachment = await session.scalar(
                    select(ConversationAttachment).where(
                        ConversationAttachment.conversation_id == conversation_id,
                        ConversationAttachment.content_sha256 == digest,
                    )
                )
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
                        )
                    )
                    if active is not None and active >= 5:
                        raise AppError(409, "SOURCE_LIMIT_EXCEEDED", "来源数量已达到上限")
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
                        ConversationAttachment.content_sha256 == digest,
                    )
                )
            if attachment is None:
                raise
            result, status_code = "EXISTING_QUEUED", 200
        assert attachment is not None
        response_attachment = AttachmentResponse.from_model(attachment)
        if result in {"CREATED", "RETRY_QUEUED"}:
            await self._put_object(attachment.object_key, content)
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
                    .where(ConversationAttachment.conversation_id == conversation_id)
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
                )
            )
            if attachment is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            return AttachmentResponse.from_model(attachment)

    async def _visible_conversation(
        self, session: AsyncSession, conversation_id: str, owner: str
    ) -> None:
        if (
            await session.scalar(
                select(Conversation.id).where(
                    Conversation.id == conversation_id, Conversation.owner_account_id == owner
                )
            )
            is None
        ):
            raise AppError(404, "NOT_FOUND", "资源不存在")

    async def _validate_pdf(self, filename: str, content_type: str, content: bytes) -> int:
        if len(content) > self._settings.attachment_max_bytes:
            raise AppError(413, "DOCUMENT_TOO_LARGE", "文档超过限制", {"reason": "BYTES_EXCEEDED"})
        if content_type != "application/pdf":
            raise AppError(422, "INVALID_PDF", "PDF 无效", {"reason": "MIME_MISMATCH"})
        if not filename.lower().endswith(".pdf"):
            raise AppError(422, "INVALID_PDF", "PDF 无效", {"reason": "EXTENSION_INVALID"})
        if not content.startswith(b"%PDF-"):
            raise AppError(422, "INVALID_PDF", "PDF 无效", {"reason": "SIGNATURE_INVALID"})
        if b"/Encrypt" in content:
            raise AppError(
                422,
                "INVALID_PDF",
                "PDF 无效",
                {"reason": "PASSWORD_PROTECTED"},
            )
        try:
            document = await asyncio.to_thread(self._convert_pdf, filename, content)
            pages = len(document.pages)
        except AppError:
            raise
        except Exception as exc:
            reason = "PAGE_COUNT_UNREADABLE" if b"/Type /Catalog" in content else "CORRUPT"
            raise AppError(422, "INVALID_PDF", "PDF 无效", {"reason": reason}) from exc
        if pages == 0:
            raise AppError(422, "INVALID_PDF", "PDF 无效", {"reason": "PAGE_COUNT_UNREADABLE"})
        if pages > self._settings.document_max_pages:
            raise AppError(413, "DOCUMENT_TOO_LARGE", "文档超过限制", {"reason": "PAGES_EXCEEDED"})
        return pages

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
                assert item is not None
                content = await self._get_object(item.object_key)
                digest = item.content_sha256
            document = await asyncio.to_thread(self._convert_pdf, item.filename, content)
            await self._progress(attachment_id, ProcessingStage.CHUNKING, 35)
            chunks = await asyncio.to_thread(self._chunk_document, document)
            await self._progress(attachment_id, ProcessingStage.EMBEDDING, 60)
            texts = [text for _, text in chunks]
            vectors = await self._embed(texts)
            await self._progress(attachment_id, ProcessingStage.INDEXING, 85)
            await self._replace_index(attachment_id, digest, chunks, vectors)
            await self._finish(attachment_id, True)
        except Exception:
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

    def _convert_pdf(self, filename: str, content: bytes):  # type: ignore[no-untyped-def]
        # Docling's converter owns model state; keep concurrent queue workers from sharing it.
        with self._converter_lock:
            result = self._converter.convert(
                DocumentStream(name=filename, stream=io.BytesIO(content))
            )
        return result.document

    @staticmethod
    def _chunk_document(document: object) -> list[tuple[list[int], str]]:
        chunks: list[tuple[list[int], str]] = []
        for chunk in cast(Any, HybridChunker().chunk(cast(Any, document))):
            text = chunk.text.strip()
            page_numbers: list[int] = sorted(
                {
                    int(provenance.page_no)
                    for item in chunk.meta.doc_items
                    for provenance in item.prov
                    if provenance.page_no is not None
                }
            )
            if text and page_numbers:
                chunks.append((page_numbers, text))
        if not chunks:
            raise ValueError("Docling produced no chunk with page provenance")
        return chunks

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        if not self._settings.embedding_base_url:
            raise RuntimeError("embedding is not configured")
        headers: dict[str, str] = {}
        if self._settings.embedding_api_key:
            headers["Authorization"] = (
                f"Bearer {self._settings.embedding_api_key.get_secret_value()}"
            )
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self._settings.embedding_base_url.rstrip('/')}/embeddings",
                headers=headers,
                json={"model": self._settings.embedding_model, "input": texts},
            )
            response.raise_for_status()
            vectors: list[list[float]] = [item["embedding"] for item in response.json()["data"]]
        if len(vectors) != len(texts) or any(
            len(vector) != self._settings.embedding_dimensions for vector in vectors
        ):
            raise RuntimeError("embedding dimension mismatch")
        return vectors

    async def _replace_index(
        self,
        source_id: str,
        digest: str,
        chunks: list[tuple[list[int], str]],
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
            for chunk_number, ((page_numbers, text), vector) in enumerate(
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
                        "page_numbers": page_numbers,
                        "text": text,
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

    def _s3_headers(self, method: str, path: str, payload: bytes) -> dict[str, str]:
        parsed = urlsplit(self._settings.minio_endpoint)
        now = datetime.now(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()
        canonical_headers = (
            f"host:{parsed.netloc}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{stamp}\n"
        )
        signed = "host;x-amz-content-sha256;x-amz-date"
        request = "\n".join([method, path, "", canonical_headers, signed, payload_hash])
        scope = f"{date}/us-east-1/s3/aws4_request"
        to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(request.encode()).hexdigest()]
        )
        key = hmac.new(
            f"AWS4{self._settings.minio_secret_key.get_secret_value()}".encode(),
            date.encode(),
            hashlib.sha256,
        ).digest()
        for value in ("us-east-1", "s3", "aws4_request"):
            key = hmac.new(key, value.encode(), hashlib.sha256).digest()
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._settings.minio_access_key}/{scope},"
            f"SignedHeaders={signed},Signature={signature}"
        )
        return {
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": stamp,
            "Authorization": authorization,
        }

    async def _put_object(self, key: str, content: bytes) -> None:
        bucket = quote(self._settings.minio_bucket, safe="")
        base = self._settings.minio_endpoint.rstrip("/")
        async with httpx.AsyncClient(timeout=10) as client:
            bucket_path = f"/{bucket}"
            response = await client.put(
                f"{base}{bucket_path}",
                headers=self._s3_headers("PUT", bucket_path, b""),
                content=b"",
            )
            if response.status_code not in {200, 409}:
                response.raise_for_status()
            path = f"/{bucket}/{quote(key, safe='/')}"
            response = await client.put(
                f"{base}{path}", headers=self._s3_headers("PUT", path, content), content=content
            )
            response.raise_for_status()

    async def _get_object(self, key: str) -> bytes:
        path = f"/{quote(self._settings.minio_bucket, safe='')}/{quote(key, safe='/')}"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{self._settings.minio_endpoint.rstrip('/')}{path}",
                headers=self._s3_headers("GET", path, b""),
            )
            response.raise_for_status()
            return response.content

    async def read_source_object(self, key: str) -> bytes:
        return await self._get_object(key)
