from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.config import Settings
from knowflow.app.errors import AppError
from knowflow.knowledge.documents.models import (
    CleanupStatus,
    KnowledgeSubmission,
    KnowledgeSubmissionStatus,
)
from knowflow.knowledge.documents.object_store import PrivateObjectStore, compensate_object
from knowflow.knowledge.documents.pdf import PdfDocuments
from knowflow.knowledge.documents.schemas import (
    SubmissionListResponse,
    SubmissionResponse,
    SubmissionUploadResponse,
)


class SubmissionDocuments:
    """Knowledge submission creation and owner-list capability."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        pdf: PdfDocuments,
        objects: PrivateObjectStore,
    ) -> None:
        self._sessions = sessions
        self._max_bytes = settings.submission_max_bytes
        self._pdf = pdf
        self._objects = objects

    async def submit(
        self,
        submitter_account_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> tuple[int, SubmissionUploadResponse]:
        page_count = await self._pdf.validate(
            filename,
            content_type,
            content,
            max_bytes=self._max_bytes,
        )
        digest = hashlib.sha256(content).hexdigest()
        submission_id = f"sub_{uuid.uuid4().hex}"
        object_key = f"submissions/{submission_id}.pdf"
        now = datetime.now(UTC).replace(tzinfo=None)
        submission = KnowledgeSubmission(
            id=submission_id,
            submitter_account_id=submitter_account_id,
            reviewer_account_id=None,
            filename=filename,
            object_key=object_key,
            content_sha256=digest,
            size_bytes=len(content),
            page_count=page_count,
            status=KnowledgeSubmissionStatus.PENDING_REVIEW,
            rejection_reason=None,
            processing_stage=None,
            processing_progress=None,
            processing_error_code=None,
            active_content_sha256=None,
            enabled=True,
            disabled_at=None,
            es_cleanup_status=None,
            es_cleanup_error_code=None,
            es_cleanup_failed_at=None,
            minio_cleanup_status=None,
            minio_cleanup_error_code=None,
            minio_cleanup_failed_at=None,
            created_at=now,
            reviewed_at=None,
            published_at=None,
            deleted_at=None,
        )
        object_may_exist = False
        try:
            async with self._sessions() as session, session.begin():
                active_id = await session.scalar(
                    select(KnowledgeSubmission.id)
                    .where(
                        KnowledgeSubmission.active_content_sha256 == digest,
                        KnowledgeSubmission.deleted_at.is_(None),
                    )
                    .limit(1)
                )
                if active_id is not None:
                    return 200, SubmissionUploadResponse(result="ALREADY_ACTIVE")
                session.add(submission)
                await session.flush()
                object_may_exist = True
                await self._objects.put(object_key, content)
        except Exception:
            if object_may_exist:
                await self._compensate_object(object_key)
            raise
        return 201, SubmissionUploadResponse(
            result="CREATED",
            submission=SubmissionResponse.from_model(submission),
        )

    async def list_for_owner(self, submitter_account_id: str) -> SubmissionListResponse:
        async with self._sessions() as session:
            items = (
                await session.scalars(
                    select(KnowledgeSubmission)
                    .where(
                        KnowledgeSubmission.submitter_account_id == submitter_account_id,
                        KnowledgeSubmission.deleted_at.is_(None),
                    )
                    .order_by(
                        KnowledgeSubmission.created_at.desc(),
                        KnowledgeSubmission.id.desc(),
                    )
                )
            ).all()
        return SubmissionListResponse(items=[SubmissionResponse.from_model(item) for item in items])

    async def list_for_admin(self) -> SubmissionListResponse:
        async with self._sessions() as session:
            items = (
                await session.scalars(
                    select(KnowledgeSubmission).where(KnowledgeSubmission.deleted_at.is_(None))
                    .order_by(
                        KnowledgeSubmission.created_at.desc(),
                        KnowledgeSubmission.id.desc(),
                    )
                )
            ).all()
        return SubmissionListResponse(items=[SubmissionResponse.from_model(item) for item in items])

    async def read_pending_document(self, submission_id: str) -> tuple[bytes, str]:
        async with self._sessions() as session:
            submission = await session.scalar(
                select(KnowledgeSubmission).where(
                    KnowledgeSubmission.id == submission_id,
                    KnowledgeSubmission.deleted_at.is_(None),
                )
            )
            if submission is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            if submission.status != KnowledgeSubmissionStatus.PENDING_REVIEW:
                raise AppError(409, "STATE_CONFLICT", "当前状态不允许此操作")
            object_key = submission.object_key
            filename = submission.filename
        try:
            return await self._objects.get(object_key), filename
        except Exception as exc:
            logging.getLogger("knowflow.documents").error(
                "Submission document read failed",
                extra={"exception_type": type(exc).__name__},
            )
            raise AppError(500, "INTERNAL_ERROR", "服务暂时不可用") from exc

    async def reject(self, submission_id: str, reviewer_account_id: str) -> SubmissionResponse:
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
            if submission.status == KnowledgeSubmissionStatus.REJECTED:
                return SubmissionResponse.from_model(submission)
            if submission.status != KnowledgeSubmissionStatus.PENDING_REVIEW:
                raise AppError(409, "STATE_CONFLICT", "当前状态不允许此操作")
            submission.status = KnowledgeSubmissionStatus.REJECTED
            submission.reviewer_account_id = reviewer_account_id
            submission.rejection_reason = "管理员审核拒绝"
            submission.reviewed_at = datetime.now(UTC).replace(tzinfo=None)
            await session.flush()
            return SubmissionResponse.from_model(submission)

    async def delete(self, submission_id: str, owner_account_id: str) -> None:
        async with self._sessions() as session, session.begin():
            submission = await session.scalar(
                select(KnowledgeSubmission)
                .where(
                    KnowledgeSubmission.id == submission_id,
                    KnowledgeSubmission.submitter_account_id == owner_account_id,
                )
                .with_for_update()
            )
            if submission is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            if submission.deleted_at is not None:
                return
            if submission.status == KnowledgeSubmissionStatus.PUBLISHING:
                raise AppError(409, "STATE_CONFLICT", "当前状态不允许此操作")
            if submission.status == KnowledgeSubmissionStatus.PUBLISHED:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            submission.deleted_at = datetime.now(UTC).replace(tzinfo=None)
            submission.es_cleanup_status = CleanupStatus.PENDING
            submission.minio_cleanup_status = CleanupStatus.PENDING

    async def delete_admin(self, submission_id: str) -> None:
        async with self._sessions() as session, session.begin():
            submission = await session.scalar(
                select(KnowledgeSubmission)
                .where(
                    KnowledgeSubmission.id == submission_id,
                )
                .with_for_update()
            )
            if submission is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            if submission.deleted_at is not None:
                return
            if submission.status in {
                KnowledgeSubmissionStatus.PUBLISHING,
                KnowledgeSubmissionStatus.PUBLISHED,
            }:
                raise AppError(409, "STATE_CONFLICT", "当前状态不允许此操作")
            submission.deleted_at = datetime.now(UTC).replace(tzinfo=None)
            submission.es_cleanup_status = CleanupStatus.PENDING
            submission.minio_cleanup_status = CleanupStatus.PENDING

    async def _compensate_object(self, object_key: str) -> None:
        await compensate_object(self._objects, object_key)
