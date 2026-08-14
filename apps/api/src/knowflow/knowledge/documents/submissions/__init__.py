from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.config import Settings
from knowflow.knowledge.documents.models import (
    KnowledgeSubmission,
    KnowledgeSubmissionStatus,
)
from knowflow.knowledge.documents.object_store import PrivateObjectStore
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
            created_at=now,
            reviewed_at=None,
            published_at=None,
        )
        object_may_exist = False
        try:
            async with self._sessions() as session, session.begin():
                active_id = await session.scalar(
                    select(KnowledgeSubmission.id)
                    .where(KnowledgeSubmission.active_content_sha256 == digest)
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
                    .where(KnowledgeSubmission.submitter_account_id == submitter_account_id)
                    .order_by(
                        KnowledgeSubmission.created_at.desc(),
                        KnowledgeSubmission.id.desc(),
                    )
                )
            ).all()
        return SubmissionListResponse(items=[SubmissionResponse.from_model(item) for item in items])

    async def _compensate_object(self, object_key: str) -> None:
        exception_type = "Exception"
        for _attempt in range(3):
            try:
                await self._objects.delete(object_key)
                return
            except Exception as exc:
                exception_type = type(exc).__name__
        logging.getLogger("knowflow.documents").error(
            "Submission object compensation failed",
            extra={"exception_type": exception_type},
        )
