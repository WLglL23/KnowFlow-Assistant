from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import quote

import httpx
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.config import Settings
from knowflow.app.errors import AppError
from knowflow.knowledge.documents.models import (
    CleanupStatus,
    KnowledgeSubmission,
    KnowledgeSubmissionStatus,
    ProcessingStage,
)
from knowflow.knowledge.documents.object_store import PrivateObjectStore
from knowflow.knowledge.documents.pdf import PdfDocuments
from knowflow.knowledge.documents.schemas import (
    KnowledgeSourceListResponse,
    KnowledgeSourceResponse,
    SubmissionResponse,
)


class PublicationDocuments:
    """In-process publication and published-source management capability."""

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
        self._approval_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def stop(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def approve(self, submission_id: str, reviewer_id: str) -> SubmissionResponse:
        async with self._approval_lock:
            try:
                async with self._sessions() as session, session.begin():
                    submission = await session.scalar(
                        select(KnowledgeSubmission)
                        .where(
                            KnowledgeSubmission.id == submission_id,
                            KnowledgeSubmission.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if submission is None:
                        raise AppError(404, "NOT_FOUND", "资源不存在")
                    if submission.status in {
                        KnowledgeSubmissionStatus.PUBLISHING,
                        KnowledgeSubmissionStatus.PUBLISHED,
                        KnowledgeSubmissionStatus.PUBLISH_FAILED,
                    }:
                        return SubmissionResponse.from_model(submission)
                    if submission.status != KnowledgeSubmissionStatus.PENDING_REVIEW:
                        raise AppError(409, "STATE_CONFLICT", "当前状态不允许此操作")
                    publishing = await session.scalar(
                        select(func.count())
                        .select_from(KnowledgeSubmission)
                        .where(KnowledgeSubmission.status == KnowledgeSubmissionStatus.PUBLISHING)
                    )
                    if publishing:
                        raise AppError(
                            429,
                            "PUBLICATION_CAPACITY_EXCEEDED",
                            "当前有资料正在发布，请稍后重试",
                        )
                    now = datetime.now(UTC).replace(tzinfo=None)
                    submission.status = KnowledgeSubmissionStatus.PUBLISHING
                    submission.active_content_sha256 = submission.content_sha256
                    submission.reviewer_account_id = reviewer_id
                    submission.reviewed_at = now
                    submission.processing_stage = ProcessingStage.PARSING
                    submission.processing_progress = 10
                    submission.processing_error_code = None
                    await session.flush()
                    response = SubmissionResponse.from_model(submission)
            except IntegrityError as exc:
                raise AppError(409, "STATE_CONFLICT", "相同内容已在发布或已发布") from exc

            task = asyncio.create_task(self._publish(submission_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return response

    async def list_sources(self) -> KnowledgeSourceListResponse:
        async with self._sessions() as session:
            items = (
                await session.scalars(
                    select(KnowledgeSubmission).where(
                        KnowledgeSubmission.status == KnowledgeSubmissionStatus.PUBLISHED,
                        KnowledgeSubmission.deleted_at.is_(None),
                    )
                )
            ).all()
        return KnowledgeSourceListResponse(
            items=[KnowledgeSourceResponse.from_model(item) for item in items]
        )

    async def list_available_sources(self) -> KnowledgeSourceListResponse:
        async with self._sessions() as session:
            items = (
                await session.scalars(
                    select(KnowledgeSubmission)
                    .where(
                        KnowledgeSubmission.status == KnowledgeSubmissionStatus.PUBLISHED,
                        KnowledgeSubmission.enabled.is_(True),
                        KnowledgeSubmission.deleted_at.is_(None),
                    )
                    .order_by(KnowledgeSubmission.created_at, KnowledgeSubmission.id)
                )
            ).all()
        return KnowledgeSourceListResponse(
            items=[KnowledgeSourceResponse.from_model(item) for item in items]
        )

    async def read_source_document(self, source_id: str) -> tuple[bytes, str]:
        async with self._sessions() as session:
            source = await session.scalar(
                select(KnowledgeSubmission).where(
                    KnowledgeSubmission.id == source_id,
                    KnowledgeSubmission.deleted_at.is_(None),
                )
            )
            if source is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            if source.status != KnowledgeSubmissionStatus.PUBLISHED:
                raise AppError(409, "STATE_CONFLICT", "当前状态不允许此操作")
            object_key, filename = source.object_key, source.filename
        try:
            return await self._objects.get(object_key), filename
        except Exception as exc:
            logging.getLogger("knowflow.documents").error(
                "Published source document read failed",
                extra={"exception_type": type(exc).__name__},
            )
            raise AppError(500, "INTERNAL_ERROR", "服务暂时不可用") from exc

    async def set_enabled(self, source_id: str, enabled: bool) -> KnowledgeSourceResponse:
        async with self._sessions() as session, session.begin():
            source = await session.scalar(
                select(KnowledgeSubmission)
                .where(
                    KnowledgeSubmission.id == source_id,
                    KnowledgeSubmission.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if source is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            if source.status != KnowledgeSubmissionStatus.PUBLISHED:
                raise AppError(409, "STATE_CONFLICT", "当前状态不允许此操作")
            if source.enabled != enabled:
                source.enabled = enabled
                source.disabled_at = None if enabled else datetime.now(UTC).replace(tzinfo=None)
            await session.flush()
            return KnowledgeSourceResponse.from_model(source)

    async def delete_source(self, submission_id: str) -> None:
        async with self._sessions() as session, session.begin():
            source = await session.scalar(
                select(KnowledgeSubmission)
                .where(KnowledgeSubmission.id == submission_id)
                .with_for_update()
            )
            if source is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            if source.deleted_at is not None:
                return
            if source.status != KnowledgeSubmissionStatus.PUBLISHED:
                raise AppError(409, "STATE_CONFLICT", "当前状态不允许此操作")
            source.deleted_at = datetime.now(UTC).replace(tzinfo=None)
            source.active_content_sha256 = None
            source.es_cleanup_status = CleanupStatus.PENDING
            source.minio_cleanup_status = CleanupStatus.PENDING

    async def _publish(self, submission_id: str) -> None:
        try:
            async with self._sessions() as session:
                submission = await session.get(KnowledgeSubmission, submission_id)
                if submission is None or submission.status != KnowledgeSubmissionStatus.PUBLISHING:
                    return
                filename = submission.filename
                object_key = submission.object_key
                digest = submission.content_sha256
            content = await self._objects.get(object_key)
            document = await asyncio.to_thread(self._pdf.convert, filename, content)
            await self._progress(submission_id, ProcessingStage.CHUNKING, 35)
            chunks = await asyncio.to_thread(self._chunk_document, document)
            await self._progress(submission_id, ProcessingStage.EMBEDDING, 60)
            vectors = await self._embed([text for _pages, text in chunks])
            await self._progress(submission_id, ProcessingStage.INDEXING, 85)
            await self._ensure_index()
            await self._replace_index(submission_id, digest, chunks, vectors)
            await self._finish(submission_id, success=True)
        except Exception as exc:
            logging.getLogger("knowflow.documents").error(
                "Knowledge publication failed",
                extra={"exception_type": type(exc).__name__},
            )
            await self._finish(submission_id, success=False)

    async def _progress(
        self, submission_id: str, stage: ProcessingStage, progress: int
    ) -> None:
        async with self._sessions() as session, session.begin():
            submission = await session.get(KnowledgeSubmission, submission_id)
            if submission is not None and submission.status == KnowledgeSubmissionStatus.PUBLISHING:
                submission.processing_stage = stage
                submission.processing_progress = progress

    async def _finish(self, submission_id: str, *, success: bool) -> None:
        async with self._sessions() as session, session.begin():
            submission = await session.scalar(
                select(KnowledgeSubmission)
                .where(KnowledgeSubmission.id == submission_id)
                .with_for_update()
            )
            if submission is None or submission.status != KnowledgeSubmissionStatus.PUBLISHING:
                return
            now = datetime.now(UTC).replace(tzinfo=None)
            if success:
                submission.status = KnowledgeSubmissionStatus.PUBLISHED
                submission.enabled = True
                submission.disabled_at = None
                submission.processing_stage = None
                submission.processing_progress = 100
                submission.processing_error_code = None
                submission.published_at = now
                return
            submission.status = KnowledgeSubmissionStatus.PUBLISH_FAILED
            submission.active_content_sha256 = None
            submission.processing_error_code = "PUBLICATION_FAILED"
            submission.es_cleanup_status = CleanupStatus.PENDING
            submission.es_cleanup_error_code = None
            submission.es_cleanup_failed_at = None
            submission.minio_cleanup_status = None
            submission.minio_cleanup_error_code = None
            submission.minio_cleanup_failed_at = None
            submission.published_at = None

    @staticmethod
    def _chunk_document(document: object) -> list[tuple[list[int], str]]:
        chunks: list[tuple[list[int], str]] = []
        for chunk in cast(Any, HybridChunker().chunk(cast(Any, document))):
            text = chunk.text.strip()
            pages = sorted(
                {
                    int(provenance.page_no)
                    for item in chunk.meta.doc_items
                    for provenance in item.prov
                    if provenance.page_no is not None
                }
            )
            if text and pages:
                chunks.append((pages, text))
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

    async def _ensure_index(self) -> None:
        base = self._settings.elasticsearch_url.rstrip("/")
        index = self._settings.elasticsearch_index
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

    async def _replace_index(
        self,
        source_id: str,
        digest: str,
        chunks: list[tuple[list[int], str]],
        vectors: list[list[float]],
    ) -> None:
        base = self._settings.elasticsearch_url.rstrip("/")
        index = self._settings.elasticsearch_index
        async with httpx.AsyncClient(timeout=10) as client:
            deleted = await client.post(
                f"{base}/{index}/_delete_by_query",
                params={"refresh": "true", "conflicts": "proceed"},
                json={"query": {"term": {"source_id": source_id}}},
            )
            deleted.raise_for_status()
            for number, ((pages, text), vector) in enumerate(zip(chunks, vectors), start=1):
                chunk_id = f"{source_id}:{number}"
                indexed = await client.put(
                    f"{base}/{index}/_doc/{quote(chunk_id, safe='')}",
                    params={"refresh": "true"},
                    json={
                        "source_type": "KNOWLEDGE",
                        "source_id": source_id,
                        "content_sha256": digest,
                        "chunk_id": chunk_id,
                        "page_numbers": pages,
                        "text": text,
                        "vector": vector,
                    },
                )
                indexed.raise_for_status()
