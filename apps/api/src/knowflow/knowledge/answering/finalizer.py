from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.knowledge.answering.models import (
    AgentRun,
    AnswerRoute,
    Message,
    MessageStatus,
    RunStatus,
)
from knowflow.knowledge.answering.outcomes import AnsweringOutcome
from knowflow.knowledge.conversations.models import Conversation


class AnsweringFinalizer:
    """Own terminal persistence; callers publish events only after it returns."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def commit_success(
        self,
        outcome: AnsweringOutcome,
        *,
        conversation_id: str,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        finished_at: datetime,
        trace_json: dict[str, object],
        trace_version: str,
        policy_version: str,
    ) -> None:
        self._validate_success(outcome)
        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation)
                .where(Conversation.id == conversation_id)
                .with_for_update()
            )
            run = await session.get(AgentRun, run_id, with_for_update=True)
            user_message = await session.get(Message, user_message_id)
            assistant_message = await session.get(
                Message, assistant_message_id, with_for_update=True
            )
            if (
                conversation is None
                or run is None
                or user_message is None
                or assistant_message is None
            ):
                raise RuntimeError("Answering terminal state is missing")
            earlier_success = await session.scalar(
                select(AgentRun.id)
                .where(
                    AgentRun.conversation_id == conversation_id,
                    AgentRun.id != run_id,
                    AgentRun.status == RunStatus.SUCCEEDED,
                )
                .limit(1)
            )
            run.status = RunStatus(outcome.run_status)
            run.route = AnswerRoute(outcome.route)
            run.error_code = None
            run.finished_at = finished_at
            run.trace_version = trace_version
            run.policy_version = policy_version
            run.trace_json = trace_json
            assistant_message.content = outcome.answer
            assistant_message.status = MessageStatus.COMPLETE
            assistant_message.citations_json = outcome.citations
            if earlier_success is None and conversation.title == "新会话":
                conversation.title = user_message.content[:20]
                conversation.updated_at = max(
                    finished_at,
                    conversation.updated_at + timedelta(microseconds=1),
                )

    async def commit_failure(
        self,
        outcome: AnsweringOutcome,
        *,
        run_id: str,
        assistant_message_id: str,
        finished_at: datetime,
        trace_json: dict[str, object],
        trace_version: str,
        policy_version: str,
        error_code: str,
    ) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get(AgentRun, run_id, with_for_update=True)
            assistant_message = await session.get(
                Message, assistant_message_id, with_for_update=True
            )
            if run is None or assistant_message is None:
                raise RuntimeError("Answering failure state is missing")
            if run.status != RunStatus.RUNNING:
                return
            run.status = RunStatus.FAILED
            run.route = AnswerRoute(outcome.route)
            run.error_code = error_code
            run.finished_at = finished_at
            run.trace_version = trace_version
            run.policy_version = policy_version
            run.trace_json = trace_json
            assistant_message.content = outcome.answer
            assistant_message.status = MessageStatus.PARTIAL
            assistant_message.citations_json = []

    @staticmethod
    def _validate_success(outcome: AnsweringOutcome) -> None:
        if not outcome.answer.strip():
            raise ValueError("successful answering outcome requires visible content")
        if outcome.route == "GENERAL" and outcome.citations:
            raise ValueError("general answer cannot persist citations")
        if outcome.route == "RETRIEVAL" and any(
            item.status == "ANSWERED" and not item.answer_executed
            for item in outcome.subquestions
        ):
            raise ValueError("answered subquestion must record answer execution")
