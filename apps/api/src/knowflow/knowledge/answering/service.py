from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.errors import AppError
from knowflow.knowledge.answering.chat import (
    ChatClient,
    ChatError,
    ChatMessage,
)
from knowflow.knowledge.answering.context import (
    HistoricalMessage,
    PreparedContext,
    prepare_context,
    summary_messages,
)
from knowflow.knowledge.answering.contracts import (
    AcceptedTurn,
    AnswerDeltaEvent,
    AnsweringEvent,
    RunCompletedEvent,
    RunFailedEvent,
    StageEvent,
)
from knowflow.knowledge.answering.models import (
    AgentRun,
    AgentRunStage,
    AnswerRoute,
    Message,
    MessageRole,
    MessageStatus,
    RunStatus,
    StageName,
    StageStatus,
)
from knowflow.knowledge.conversations.models import Conversation


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: AnswerRoute
    standalone_question: str
    reason_code: str


class AnsweringService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        chat: ChatClient,
    ) -> None:
        self._sessions = sessions
        self._chat = chat

    async def accept_turn(
        self,
        *,
        actor_id: str,
        conversation_id: str,
        content: str,
        client_request_id: str,
    ) -> AcceptedTurn:
        await self._require_owner(actor_id, conversation_id)
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("content must not be empty")
        self._chat.ensure_configured()

        user_message_id = self._id("msg")
        assistant_message_id = self._id("msg")
        run_id = self._id("run")
        request_id = self._id("req")
        now = self._utc_now()

        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.owner_account_id == actor_id,
                )
                .with_for_update()
            )
            if conversation is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            user_created_at = now
            assistant_created_at = now + timedelta(microseconds=1)
            session.add_all(
                [
                    Message(
                        id=user_message_id,
                        conversation_id=conversation_id,
                        role=MessageRole.USER,
                        content=normalized_content,
                        status=MessageStatus.COMPLETE,
                        citations_json=[],
                        created_at=user_created_at,
                    ),
                    Message(
                        id=assistant_message_id,
                        conversation_id=conversation_id,
                        role=MessageRole.ASSISTANT,
                        content="",
                        status=MessageStatus.PENDING,
                        citations_json=[],
                        created_at=assistant_created_at,
                    ),
                ]
            )
            await session.flush()
            session.add(
                AgentRun(
                    id=run_id,
                    actor_account_id=actor_id,
                    conversation_id=conversation_id,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    client_request_id=client_request_id,
                    status=RunStatus.RUNNING,
                    route=None,
                    source_scope_json={},
                    trace_version=None,
                    policy_version=None,
                    trace_json={},
                    error_code=None,
                    started_at=assistant_created_at,
                    finished_at=None,
                )
            )
            conversation.updated_at = max(
                assistant_created_at,
                conversation.updated_at + timedelta(microseconds=1),
            )

        accepted = AcceptedTurn(
            conversation_id=conversation_id,
            run_id=run_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            request_id=request_id,
            _event_factory=lambda: self._events(
                conversation_id=conversation_id,
                run_id=run_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                request_id=request_id,
                content=normalized_content,
            ),
        )
        return accepted

    async def _events(
        self,
        *,
        conversation_id: str,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        request_id: str,
        content: str,
    ) -> AsyncIterator[AnsweringEvent]:
        answer_parts: list[str] = []
        try:
            understand_stage_id = await self._start_stage(run_id, StageName.UNDERSTAND)
        except Exception:
            await self._converge_failure(
                run_id, assistant_message_id, answer_parts, "INTERNAL_ERROR"
            )
            yield self._failed_event(
                request_id, run_id, assistant_message_id, "INTERNAL_ERROR"
            )
            return
        yield StageEvent("stage.started", request_id, run_id, StageName.UNDERSTAND.value)

        try:
            prepared = await self._prepare(conversation_id, user_message_id)
            if prepared.summary_candidates:
                prepared = await self._summarize(conversation_id, prepared)
            decision = await self._route(prepared, content)
            await self._complete_understand_stage(understand_stage_id, run_id, decision.route)
        except Exception:
            await self._fail_stage(understand_stage_id)
            await self._converge_failure(run_id, assistant_message_id, answer_parts, "MODEL_FAILED")
            yield self._failed_event(
                request_id, run_id, assistant_message_id, "MODEL_FAILED"
            )
            return

        yield StageEvent("stage.completed", request_id, run_id, StageName.UNDERSTAND.value)
        try:
            generate_stage_id = await self._start_stage(run_id, StageName.GENERATE)
        except Exception:
            await self._converge_failure(
                run_id, assistant_message_id, answer_parts, "INTERNAL_ERROR"
            )
            yield self._failed_event(
                request_id, run_id, assistant_message_id, "INTERNAL_ERROR"
            )
            return
        yield StageEvent("stage.started", request_id, run_id, StageName.GENERATE.value)

        try:
            if decision.route is AnswerRoute.RETRIEVAL:
                refusal = "当前没有可用资料。请上传附件或开启并选择知识源后再试。"
                answer_parts.append(refusal)
                yield AnswerDeltaEvent("answer.delta", request_id, run_id, refusal)
            else:
                async for delta in self._generate(prepared, content, decision):
                    if not delta:
                        continue
                    answer_parts.append(delta)
                    yield AnswerDeltaEvent("answer.delta", request_id, run_id, delta)
            await self._complete_stage(generate_stage_id)
        except Exception:
            await self._fail_stage(generate_stage_id)
            await self._converge_failure(run_id, assistant_message_id, answer_parts, "MODEL_FAILED")
            yield self._failed_event(
                request_id, run_id, assistant_message_id, "MODEL_FAILED"
            )
            return

        yield StageEvent("stage.completed", request_id, run_id, StageName.GENERATE.value)
        try:
            await self._converge_success(
                conversation_id=conversation_id,
                run_id=run_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                content="".join(answer_parts),
            )
        except Exception:
            await self._converge_failure(
                run_id, assistant_message_id, answer_parts, "INTERNAL_ERROR"
            )
            yield self._failed_event(
                request_id, run_id, assistant_message_id, "INTERNAL_ERROR"
            )
            return

        yield RunCompletedEvent("run.completed", request_id, run_id, assistant_message_id)

    async def _require_owner(self, actor_id: str, conversation_id: str) -> None:
        async with self._sessions() as session:
            visible_id = await session.scalar(
                select(Conversation.id).where(
                    Conversation.id == conversation_id,
                    Conversation.owner_account_id == actor_id,
                )
            )
        if visible_id is None:
            raise AppError(404, "NOT_FOUND", "资源不存在")

    async def _prepare(
        self, conversation_id: str, current_user_message_id: str
    ) -> PreparedContext:
        async with self._sessions() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise RuntimeError("Accepted conversation no longer exists")
            history = list(
                (
                    await session.scalars(
                        select(Message)
                        .where(
                            Message.conversation_id == conversation_id,
                            Message.status == MessageStatus.COMPLETE,
                            Message.id != current_user_message_id,
                        )
                        .order_by(Message.created_at.asc(), Message.id.asc())
                    )
                ).all()
            )
            if conversation.summary_through_message_id is not None:
                for index, message in enumerate(history):
                    if message.id == conversation.summary_through_message_id:
                        history = history[index + 1 :]
                        break
            context_history = [
                HistoricalMessage(
                    id=message.id,
                    role=message.role.value,
                    content=message.content,
                )
                for message in history
            ]
            return prepare_context(context_history, summary_text=conversation.summary_text)

    async def _summarize(
        self, conversation_id: str, prepared: PreparedContext
    ) -> PreparedContext:
        try:
            summary = await self._chat.complete_text(summary_messages(prepared))
        except ChatError:
            return prepared
        cursor = prepared.summary_candidates[-1].id
        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if conversation is None:
                raise RuntimeError("Accepted conversation no longer exists")
            conversation.summary_text = summary
            conversation.summary_through_message_id = cursor
        return PreparedContext(
            summary_text=summary,
            recent_messages=prepared.recent_messages,
            summary_candidates=prepared.summary_candidates,
        )

    async def _route(self, prepared: PreparedContext, content: str) -> RouteDecision:
        messages = [
            ChatMessage(
                "system",
                "判断问题应走 GENERAL 还是 RETRIEVAL。只返回 JSON："
                '{"route":"GENERAL|RETRIEVAL","standalone_question":"...",'
                '"reason_code":"..."}。明确要求 PDF 或资料证据时选择 RETRIEVAL。',
            ),
            *prepared.chat_messages(content),
        ]
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                payload = await self._chat.complete_json(messages)
                return RouteDecision.model_validate(payload)
            except (ChatError, ValueError) as exc:
                last_error = exc
        raise ChatError("Router output failed validation") from last_error

    async def _generate(
        self,
        prepared: PreparedContext,
        content: str,
        decision: RouteDecision,
    ) -> AsyncIterator[str]:
        messages = [
            ChatMessage("system", "直接回答用户问题。不要声称检索或引用任何 PDF。"),
            *prepared.chat_messages(content),
            ChatMessage("system", f"独立问题：{decision.standalone_question}"),
        ]
        async for delta in self._chat.stream_text(messages):
            yield delta

    async def _start_stage(self, run_id: str, stage: StageName) -> str:
        stage_id = self._id("stg")
        async with self._sessions() as session, session.begin():
            session.add(
                AgentRunStage(
                    id=stage_id,
                    run_id=run_id,
                    stage=stage,
                    status=StageStatus.RUNNING,
                    started_at=self._utc_now(),
                    finished_at=None,
                    summary_json={},
                )
            )
        return stage_id

    async def _complete_understand_stage(
        self, stage_id: str, run_id: str, route: AnswerRoute
    ) -> None:
        async with self._sessions() as session, session.begin():
            stage = await session.get(AgentRunStage, stage_id)
            run = await session.get(AgentRun, run_id)
            if stage is None or run is None:
                raise RuntimeError("Answering state is missing")
            stage.status = StageStatus.SUCCEEDED
            stage.finished_at = self._utc_now()
            run.route = route

    async def _complete_stage(self, stage_id: str) -> None:
        async with self._sessions() as session, session.begin():
            stage = await session.get(AgentRunStage, stage_id)
            if stage is None:
                raise RuntimeError("Answering stage is missing")
            stage.status = StageStatus.SUCCEEDED
            stage.finished_at = self._utc_now()

    async def _fail_stage(self, stage_id: str) -> None:
        async with self._sessions() as session, session.begin():
            stage = await session.get(AgentRunStage, stage_id)
            if stage is not None and stage.status == StageStatus.RUNNING:
                stage.status = StageStatus.FAILED
                stage.finished_at = self._utc_now()

    async def _converge_success(
        self,
        *,
        conversation_id: str,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        content: str,
    ) -> None:
        now = self._utc_now()
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
            run.status = RunStatus.SUCCEEDED
            run.error_code = None
            run.finished_at = now
            assistant_message.content = content
            assistant_message.status = MessageStatus.COMPLETE
            if earlier_success is None and conversation.title == "新会话":
                conversation.title = user_message.content[:20]
                conversation.updated_at = max(
                    now,
                    conversation.updated_at + timedelta(microseconds=1),
                )

    async def _converge_failure(
        self,
        run_id: str,
        assistant_message_id: str,
        answer_parts: list[str],
        error_code: str,
    ) -> None:
        async with self._sessions() as session, session.begin():
            run = await session.get(AgentRun, run_id, with_for_update=True)
            assistant_message = await session.get(
                Message, assistant_message_id, with_for_update=True
            )
            if run is None or assistant_message is None:
                raise RuntimeError("Answering failure state is missing")
            run.status = RunStatus.FAILED
            run.error_code = error_code
            run.finished_at = self._utc_now()
            assistant_message.content = "".join(answer_parts)
            assistant_message.status = MessageStatus.PARTIAL

    @staticmethod
    def _failed_event(
        request_id: str,
        run_id: str,
        assistant_message_id: str,
        error_code: str,
    ) -> RunFailedEvent:
        message = "回答生成失败" if error_code == "MODEL_FAILED" else "回答处理失败"
        return RunFailedEvent(
            "run.failed",
            request_id,
            run_id,
            assistant_message_id,
            {"code": error_code, "message": message},
        )

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
