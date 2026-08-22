from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.config import Settings
from knowflow.app.errors import AppError
from knowflow.knowledge.answering.models import AgentRun, RunStatus
from knowflow.knowledge.conversations.models import Conversation
from knowflow.knowledge.documents.models import (
    AttachmentStatus,
    CleanupStatus,
    ConversationAttachment,
    KnowledgeSubmission,
    KnowledgeSubmissionStatus,
)
from knowflow.knowledge.documents.object_store import PrivateObjectStore

logger = logging.getLogger("knowflow.documents.cleanup")


class CleanupDocuments:
    """Small row-backed cleanup capability for deleted document resources."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        objects: PrivateObjectStore,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._objects = objects

    async def list_failures(self) -> dict[str, list[dict[str, object]]]:
        async with self._sessions() as session:
            attachments = (
                await session.scalars(
                    select(ConversationAttachment).where(
                        ConversationAttachment.deleted_at.is_not(None)
                    )
                )
            ).all()
            submissions = (
                await session.scalars(
                    select(KnowledgeSubmission).where(
                        (KnowledgeSubmission.deleted_at.is_not(None))
                        | (
                            KnowledgeSubmission.status
                            == KnowledgeSubmissionStatus.PUBLISH_FAILED
                        )
                    )
                )
            ).all()
        items: list[dict[str, object]] = []
        for item in attachments:
            items.extend(self._failure_items("ATTACHMENT", item.id, item))
        for item in submissions:
            items.extend(self._failure_items("SUBMISSION", item.id, item))
        return {"items": items}

    async def retry(self, resource_type: str, resource_id: str) -> dict[str, object]:
        normalized = self._normalize_type(resource_type)
        resource = await self._load_resource(normalized, resource_id)
        if resource is None:
            raise AppError(404, "NOT_FOUND", "资源不存在")
        ran = await self._run_pending_steps(normalized, resource)
        if not ran:
            raise AppError(409, "STATE_CONFLICT", "当前状态不允许此操作")
        resource = await self._load_resource(normalized, resource_id)
        assert resource is not None
        return self._resource_snapshot(normalized, resource)

    async def on_run_terminal(self, run_id: str) -> None:
        async with self._sessions() as session:
            run = await session.get(AgentRun, run_id)
        if run is None or run.status == RunStatus.RUNNING:
            return
        scope = run.source_scope_json
        for resource_id in [
            *scope.get("attachment_ids", []),
            *scope.get("knowledge_source_ids", []),
        ]:
            if not isinstance(resource_id, str):
                continue
            resource_type = "ATTACHMENT" if resource_id.startswith("att_") else "SUBMISSION"
            try:
                resource = await self._load_resource(resource_type, resource_id)
                if resource is not None:
                    await self._run_pending_steps(resource_type, resource)
            except Exception:
                logger.exception("Terminal cleanup failed", extra={"run_id": run_id})

    async def recover_startup(self) -> None:
        async with self._sessions() as session, session.begin():
            attachments = (
                await session.scalars(
                    select(ConversationAttachment)
                    .where(ConversationAttachment.status == AttachmentStatus.PROCESSING)
                    .with_for_update()
                )
            ).all()
            for item in attachments:
                if item.deleted_at is not None:
                    item.status = AttachmentStatus.FAILED
                    item.processing_error_code = "ATTACHMENT_DELETED_DURING_PROCESSING"
                    item.processed_at = self._now()
                else:
                    item.status = AttachmentStatus.QUEUED
                item.processing_started_at = None
                item.processing_stage = None
                item.processing_progress = None

            submissions = (
                await session.scalars(
                    select(KnowledgeSubmission)
                    .where(KnowledgeSubmission.status == KnowledgeSubmissionStatus.PUBLISHING)
                    .with_for_update()
                )
            ).all()
            for item in submissions:
                item.status = KnowledgeSubmissionStatus.PUBLISH_FAILED
                item.active_content_sha256 = None
                item.processing_error_code = "PUBLICATION_INTERRUPTED"
                item.es_cleanup_status = CleanupStatus.PENDING
                item.es_cleanup_error_code = None
                item.es_cleanup_failed_at = None
                item.minio_cleanup_status = None
                item.minio_cleanup_error_code = None
                item.minio_cleanup_failed_at = None

            for model in (ConversationAttachment, KnowledgeSubmission):
                for step in ("es", "minio"):
                    rows = (
                        await session.scalars(
                            select(model)
                            .where(
                                getattr(model, f"{step}_cleanup_status")
                                == CleanupStatus.RUNNING
                            )
                            .with_for_update()
                        )
                    ).all()
                    for item in rows:
                        setattr(item, f"{step}_cleanup_status", CleanupStatus.PENDING)
                        setattr(item, f"{step}_cleanup_error_code", None)
                        setattr(item, f"{step}_cleanup_failed_at", None)

    async def _run_pending_steps(self, resource_type: str, resource: Any) -> bool:
        ran = False
        for step in ("es", "minio"):
            status = getattr(resource, f"{step}_cleanup_status")
            if status is None or status not in {CleanupStatus.PENDING, CleanupStatus.FAILED}:
                continue
            if not await self._claim(resource_type, resource.id, step):
                continue
            ran = True
            try:
                if step == "es":
                    await self._delete_es(resource.id)
                else:
                    await self._objects.delete(resource.object_key)
            except Exception:
                logger.exception(
                    "External cleanup step failed",
                    extra={
                        "resource_type": resource_type,
                        "resource_id": resource.id,
                        "step": step,
                    },
                )
                await self._mark_step(
                    resource_type, resource.id, step, CleanupStatus.FAILED, "CLEANUP_FAILED"
                )
            else:
                await self._mark_step(
                    resource_type, resource.id, step, CleanupStatus.SUCCEEDED, None
                )
        return ran

    async def _claim(self, resource_type: str, resource_id: str, step: str) -> bool:
        async with self._sessions() as session, session.begin():
            if resource_type == "ATTACHMENT":
                resource = await session.get(ConversationAttachment, resource_id)
                if resource is None or resource.deleted_at is None:
                    return False
                await session.get(Conversation, resource.conversation_id, with_for_update=True)
                resource = await self._load_in_session(
                    session, resource_type, resource_id, lock=True
                )
            else:
                resource = await self._load_in_session(
                    session, resource_type, resource_id, lock=True
                )
            if resource is None:
                return False
            attr = f"{step}_cleanup_status"
            if getattr(resource, attr) not in {CleanupStatus.PENDING, CleanupStatus.FAILED}:
                return False
            if await self._has_active_run_in_session(session, resource_type, resource_id):
                return False
            setattr(resource, attr, CleanupStatus.RUNNING)
            return True

    async def _mark_step(
        self,
        resource_type: str,
        resource_id: str,
        step: str,
        status: CleanupStatus,
        error_code: str | None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            resource = await self._load_in_session(
                session, resource_type, resource_id, lock=True
            )
            if resource is None:
                return
            setattr(resource, f"{step}_cleanup_status", status)
            setattr(resource, f"{step}_cleanup_error_code", error_code)
            setattr(
                resource,
                f"{step}_cleanup_failed_at",
                self._now() if status == CleanupStatus.FAILED else None,
            )

    async def _load_resource(self, resource_type: str, resource_id: str) -> Any | None:
        async with self._sessions() as session:
            return await self._load_in_session(session, resource_type, resource_id)

    @staticmethod
    async def _load_in_session(
        session: AsyncSession,
        resource_type: str,
        resource_id: str,
        *,
        lock: bool = False,
    ) -> Any | None:
        model: type[Any] = (
            ConversationAttachment if resource_type == "ATTACHMENT" else KnowledgeSubmission
        )
        resource = await session.get(model, resource_id, with_for_update=lock)
        if resource is None:
            return None
        if resource_type == "ATTACHMENT" and resource.deleted_at is None:
            return None
        if (
            resource_type == "SUBMISSION"
            and resource.deleted_at is None
            and resource.status != KnowledgeSubmissionStatus.PUBLISH_FAILED
        ):
            return None
        return resource

    @staticmethod
    async def _has_active_run_in_session(
        session: AsyncSession, resource_type: str, resource_id: str
    ) -> bool:
        runs = (
            await session.scalars(select(AgentRun).where(AgentRun.status == RunStatus.RUNNING))
        ).all()
        key = "attachment_ids" if resource_type == "ATTACHMENT" else "knowledge_source_ids"
        return any(resource_id in run.source_scope_json.get(key, []) for run in runs)

    async def _delete_es(self, source_id: str) -> None:
        endpoint = self._settings.elasticsearch_url.rstrip("/")
        index = self._settings.elasticsearch_index
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{endpoint}/{index}/_delete_by_query",
                json={"query": {"term": {"source_id.keyword": source_id}}},
            )
        if response.status_code not in {200, 404}:
            response.raise_for_status()

    @staticmethod
    def _normalize_type(resource_type: str) -> str:
        normalized = resource_type.strip().upper()
        if normalized in {"SOURCE", "KNOWLEDGE_SOURCE"}:
            normalized = "SUBMISSION"
        if normalized not in {"ATTACHMENT", "SUBMISSION"}:
            raise AppError(404, "NOT_FOUND", "资源不存在")
        return normalized

    @staticmethod
    def _failure_items(
        resource_type: str, resource_id: str, resource: Any
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for step in ("es", "minio"):
            status = getattr(resource, f"{step}_cleanup_status")
            if status in {CleanupStatus.PENDING, CleanupStatus.FAILED}:
                result.append(
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                        "step": step.upper(),
                        "status": status.value,
                        "error_code": getattr(resource, f"{step}_cleanup_error_code"),
                        "failed_at": _iso(getattr(resource, f"{step}_cleanup_failed_at")),
                    }
                )
        return result

    @staticmethod
    def _resource_snapshot(resource_type: str, resource: Any) -> dict[str, object]:
        return {
            "resource_type": resource_type,
            "resource_id": resource.id,
            "steps": [
                {
                    "step": step.upper(),
                    "status": (
                        getattr(resource, f"{step}_cleanup_status").value
                        if getattr(resource, f"{step}_cleanup_status") is not None
                        else None
                    ),
                }
                for step in ("es", "minio")
            ],
        }

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
