from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.errors import AppError
from knowflow.identity.models import Account
from knowflow.knowledge.answering.chat import (
    ChatClient,
    ChatConfigurationError,
    ChatError,
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
from knowflow.knowledge.answering.diagnostics import RunDiagnostics
from knowflow.knowledge.answering.evidence_budget import (
    deduplicate_candidates,
    promote_query_anchors,
)
from knowflow.knowledge.answering.execution import RunExecutionContext as RunExecutionContext
from knowflow.knowledge.answering.execution import RunExecutionError
from knowflow.knowledge.answering.finalizer import AnsweringFinalizer
from knowflow.knowledge.answering.model_decisions import AnsweringModels
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
from knowflow.knowledge.answering.policy import RunPolicy
from knowflow.knowledge.answering.retrieval import (
    AttachmentRetrieval,
    Evidence,
)
from knowflow.knowledge.answering.retrieval_pipeline import RetrievalPipeline
from knowflow.knowledge.answering.stages import RunStages
from knowflow.knowledge.answering.subquestion_runner import (
    SubquestionAnswer,
    SubquestionRunner,
)
from knowflow.knowledge.conversations.models import Conversation
from knowflow.knowledge.conversations.service import allowed_source_scope
from knowflow.knowledge.documents.models import ConversationAttachment, KnowledgeSubmission

if TYPE_CHECKING:
    from knowflow.knowledge.documents.cleanup import CleanupDocuments


class AnsweringService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        chat: ChatClient,
        policy: RunPolicy,
        retrieval: AttachmentRetrieval,
        cleanup: CleanupDocuments | None = None,
    ) -> None:
        self._sessions = sessions
        self._chat = chat
        self._models = AnsweringModels(chat)
        self._stages = RunStages(sessions)
        self._diagnostics = RunDiagnostics(policy)
        self._policy = policy
        self._retrieval = retrieval
        self._evidence = RetrievalPipeline(retrieval, policy)
        self._cleanup = cleanup
        self._settlement_tasks: set[asyncio.Task[None]] = set()

    async def accept_turn(
        self,
        *,
        actor_id: str,
        conversation_id: str,
        content: object,
        client_request_id: object,
        request_id: str | None = None,
    ) -> AcceptedTurn:
        await self._require_owner(actor_id, conversation_id)
        normalized_content = self._validated_content(content)
        normalized_request_id = self._validated_client_request_id(client_request_id)
        try:
            self._chat.ensure_configured()
        except ChatConfigurationError as exc:
            raise AppError(500, "INTERNAL_ERROR", "服务暂时不可用") from exc

        user_message_id = self._id("msg")
        assistant_message_id = self._id("msg")
        run_id = self._id("run")
        server_request_id = request_id or self._id("req")
        now = self._utc_now()

        async with self._sessions() as session, session.begin():
            account = await session.scalar(
                select(Account).where(Account.id == actor_id).with_for_update()
            )
            if account is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            conversation = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.owner_account_id == actor_id,
                    Conversation.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if conversation is None:
                raise AppError(404, "NOT_FOUND", "资源不存在")
            duplicate = await session.scalar(
                select(AgentRun.id)
                .where(
                    AgentRun.actor_account_id == actor_id,
                    AgentRun.conversation_id == conversation_id,
                    AgentRun.client_request_id == normalized_request_id,
                )
                .limit(1)
            )
            if duplicate is not None:
                raise AppError(
                    409,
                    "REQUEST_ALREADY_ACCEPTED",
                    "该发送请求已被接受",
                )
            active = await session.scalar(
                select(AgentRun.id)
                .where(
                    AgentRun.status == RunStatus.RUNNING,
                    or_(
                        AgentRun.actor_account_id == actor_id,
                        AgentRun.conversation_id == conversation_id,
                    ),
                )
                .limit(1)
            )
            if active is not None:
                raise AppError(409, "RUN_ALREADY_ACTIVE", "当前已有回答正在运行")

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
            initial_context = RunExecutionContext(self._policy, assistant_created_at)
            session.add(
                AgentRun(
                    id=run_id,
                    actor_account_id=actor_id,
                    conversation_id=conversation_id,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    client_request_id=normalized_request_id,
                    status=RunStatus.RUNNING,
                    route=None,
                    source_scope_json=await allowed_source_scope(session, conversation),
                    trace_version=self._policy.trace_version,
                    policy_version=self._policy.policy_version,
                    trace_json=self._diagnostics.trace_without_stages(
                        initial_context, total_ms=0, termination=None
                    ),
                    error_code=None,
                    started_at=assistant_created_at,
                    finished_at=None,
                )
            )
            conversation.updated_at = max(
                assistant_created_at,
                conversation.updated_at + timedelta(microseconds=1),
            )

        return AcceptedTurn(
            conversation_id=conversation_id,
            run_id=run_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            request_id=server_request_id,
            _event_factory=lambda: self._events(
                conversation_id=conversation_id,
                run_id=run_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                request_id=server_request_id,
                content=normalized_content,
                started_at=assistant_created_at,
            ),
        )

    async def _events(
        self,
        *,
        conversation_id: str,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        request_id: str,
        content: str,
        started_at: datetime,
    ) -> AsyncIterator[AnsweringEvent]:
        context = RunExecutionContext(self._policy, started_at)
        try:
            async with asyncio.timeout(context.remaining_seconds(self._utc_now())):
                async for event in self._execute(
                    conversation_id=conversation_id,
                    run_id=run_id,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    request_id=request_id,
                    content=content,
                    context=context,
                ):
                    yield event
        except TimeoutError:
            if context.terminal_committed:
                return
            self._discard_unpublished_retrieval(context)
            await self._stages.fail_current(context)
            await self._converge_failure(
                run_id,
                assistant_message_id,
                context,
                "RUN_LIMIT_EXCEEDED",
            )
            context.terminal_committed = True
            yield self._failed_event(
                request_id,
                run_id,
                assistant_message_id,
                "RUN_LIMIT_EXCEEDED",
                observation=self._diagnostics.observation(context, "RUN_LIMIT_EXCEEDED"),
            )
        except RunExecutionError as exc:
            if context.terminal_committed:
                return
            self._discard_unpublished_retrieval(context)
            await self._stages.fail_current(context)
            await self._converge_failure(
                run_id,
                assistant_message_id,
                context,
                exc.error_code,
            )
            context.terminal_committed = True
            yield self._failed_event(
                request_id,
                run_id,
                assistant_message_id,
                exc.error_code,
                observation=self._diagnostics.observation(context, exc.error_code),
            )
        except (asyncio.CancelledError, GeneratorExit):
            if not context.terminal_committed:
                self._schedule_interruption(run_id, assistant_message_id, context)
            raise
        except Exception:
            if context.terminal_committed:
                return
            self._discard_unpublished_retrieval(context)
            await self._stages.fail_current(context)
            await self._converge_failure(
                run_id,
                assistant_message_id,
                context,
                "INTERNAL_ERROR",
            )
            context.terminal_committed = True
            yield self._failed_event(
                request_id,
                run_id,
                assistant_message_id,
                "INTERNAL_ERROR",
                observation=self._diagnostics.observation(context, "INTERNAL_ERROR"),
            )

    async def _execute(
        self,
        *,
        conversation_id: str,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        request_id: str,
        content: str,
        context: RunExecutionContext,
    ) -> AsyncIterator[AnsweringEvent]:
        pending_retrieval_delta: str | None = None
        await self._stages.enter(run_id, StageName.UNDERSTAND, context)
        yield StageEvent("stage.started", request_id, run_id, StageName.UNDERSTAND.value)

        prepared = await self._prepare(conversation_id, user_message_id)
        if prepared.summary_candidates:
            prepared = await self._summarize(conversation_id, prepared, context)
        source_filenames = await self._source_filenames(run_id)
        try:
            decision = await self._models.route(prepared, content, context, source_filenames)
        except (ChatError, ValidationError) as exc:
            raise RunExecutionError("MODEL_FAILED") from exc
        context.route = decision.route
        await self._stages.complete_understand(context.current_stage_id, run_id, decision.route)
        context.current_stage_id = None
        context.current_stage = None
        yield StageEvent("stage.completed", request_id, run_id, StageName.UNDERSTAND.value)

        if decision.route is AnswerRoute.RETRIEVAL:
            context.source_scope_compliant = True
            if source_filenames:
                context.defer_retrieval_publish = True
                await self._stages.enter(run_id, StageName.PLAN, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.PLAN.value)
                plan = await self._models.plan(prepared, decision, context, source_filenames)
                await self._stages.complete(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.PLAN.value)

                await self._stages.enter(run_id, StageName.RETRIEVE_1, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.RETRIEVE_1.value)
                runner = SubquestionRunner(
                    lambda query: self._evidence.query(query, source_filenames, context),
                    lambda query, evidence: self._answer_subquestion(query, evidence, context),
                )
                context.subquestions = []
                context.candidate_sets = []
                for query in plan.queries:
                    try:
                        retrieved = await runner.retrieve(query)
                    except RunExecutionError as exc:
                        failed = runner.new_outcome(query)
                        failed.status = "FAILED"
                        failed.failure_code = exc.error_code
                        context.subquestions.append(failed)
                        context.candidate_sets.append([])
                    else:
                        context.subquestions.append(retrieved.outcome)
                        context.candidate_sets.append(
                            promote_query_anchors(
                                f"{decision.standalone_question} {query}",
                                deduplicate_candidates(retrieved.candidates),
                            )
                        )
                self._evidence.allocate(context)
                await self._stages.complete(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.RETRIEVE_1.value)

                await self._stages.enter(run_id, StageName.VERIFY, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.VERIFY.value)
                for state in context.subquestions:
                    state.verify_input_page_hashes = {
                        hashlib.sha256(f"{item.source_id}:{page}".encode()).hexdigest()
                        for item in state.evidence
                        for page in item.pages
                    }
                    if not state.evidence:
                        state.status = "INSUFFICIENT"
                        state.missing_facts = ["足以回答该问题的 PDF 证据"]
                    elif not self._evidence.valid_evidence(
                        state.evidence, source_filenames, context
                    ):
                        state.status = "FAILED"
                        state.failure_code = "SOURCE_SCOPE_VIOLATION"
                await self._stages.complete(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.VERIFY.value)

                await self._stages.enter(run_id, StageName.GENERATE, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.GENERATE.value)
                try:
                    context.evidence = self._evidence.unique_evidence(context.subquestions)
                    context.evidence_token_count = sum(
                        self._evidence.token_count(item) for item in context.evidence
                    )
                    generated: list[tuple[str, list[Evidence]] | None] = []
                    for state in context.subquestions:
                        if state.status == "FAILED":
                            generated.append(None)
                            continue
                        try:
                            used_evidence = await runner.answer(state)
                        except RunExecutionError as exc:
                            state.status = "FAILED"
                            state.failure_code = exc.error_code
                            state.answer_executed = False
                            generated.append(None)
                            continue
                        generated.append(
                            (state.answer or "", used_evidence)
                            if used_evidence is not None and state.status == "ANSWERED"
                            else None
                        )
                    if context.subquestions and all(
                        state.status == "FAILED" for state in context.subquestions
                    ):
                        raise RunExecutionError(
                            next(
                                (
                                    state.failure_code
                                    for state in context.subquestions
                                    if state.failure_code
                                ),
                                "MODEL_FAILED",
                            )
                        )
                    answer_evidence = [
                        item for result in generated if result is not None for item in result[1]
                    ]
                    context.citations = self._evidence.citations(answer_evidence)
                    fragments: list[str] = []
                    for state, result in zip(context.subquestions, generated, strict=True):
                        if result is not None:
                            fragment, used_evidence = result
                            state.citations = [
                                citation
                                for citation in context.citations
                                if citation.get("source_id")
                                in {item.source_id for item in used_evidence}
                            ]
                            fragments.append(
                                fragment
                                + self._evidence.citation_markers(used_evidence, context.citations)
                            )
                        else:
                            missing = self._missing_facts(state.missing_facts)
                            fragments.append(f"未能确认相关内容；尚缺少：{missing}。")
                    answer = "\n\n".join(fragments)
                    visible, exceeded = self._append_visible_delta(context, answer)
                    pending_retrieval_delta = visible
                    if exceeded:
                        raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                    await self._stages.complete(context.current_stage_id)
                except ChatError as exc:
                    raise RunExecutionError("MODEL_FAILED") from exc
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.GENERATE.value)
            else:
                context.defer_retrieval_publish = True
                await self._stages.enter(run_id, StageName.GENERATE, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.GENERATE.value)
                refusal = "当前没有可用资料。请上传附件或开启并选择知识源后再试。"
                visible, exceeded = self._append_visible_delta(context, refusal)
                pending_retrieval_delta = visible
                if exceeded:
                    raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                await self._stages.complete(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.GENERATE.value)
        else:
            await self._stages.enter(run_id, StageName.GENERATE, context)
            yield StageEvent("stage.started", request_id, run_id, StageName.GENERATE.value)
            try:
                async for delta in self._models.generate(prepared, content, decision, context):
                    visible, exceeded = self._append_visible_delta(context, delta)
                    if visible:
                        yield AnswerDeltaEvent("answer.delta", request_id, run_id, visible)
                    if exceeded:
                        raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                await self._stages.complete(context.current_stage_id)
            except ChatError as exc:
                raise RunExecutionError("MODEL_FAILED") from exc
            context.current_stage_id = None
            context.current_stage = None
            yield StageEvent("stage.completed", request_id, run_id, StageName.GENERATE.value)

        try:
            await self._converge_success(
                conversation_id=conversation_id,
                run_id=run_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                context=context,
            )
        except Exception:
            self._discard_unpublished_retrieval(context)
            await self._converge_failure(
                run_id,
                assistant_message_id,
                context,
                "INTERNAL_ERROR",
            )
            context.terminal_committed = True
            yield self._failed_event(
                request_id,
                run_id,
                assistant_message_id,
                "INTERNAL_ERROR",
                observation=self._diagnostics.observation(context, "INTERNAL_ERROR"),
            )
            return
        context.terminal_committed = True
        if pending_retrieval_delta:
            yield AnswerDeltaEvent("answer.delta", request_id, run_id, pending_retrieval_delta)
        yield RunCompletedEvent(
            "run.completed",
            request_id,
            run_id,
            assistant_message_id,
            observation=self._diagnostics.observation(context, None),
        )

    async def _source_filenames(self, run_id: str) -> dict[str, str]:
        async with self._sessions() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise RuntimeError("Answering run is missing")
            filenames: dict[str, str] = {}
            for key, model in (
                ("attachment_ids", ConversationAttachment),
                ("knowledge_source_ids", KnowledgeSubmission),
            ):
                raw_ids = run.source_scope_json.get(key, [])
                if not isinstance(raw_ids, list) or not all(
                    isinstance(item, str) for item in cast(list[object], raw_ids)
                ):
                    raise RuntimeError("Answering run source scope is invalid")
                ids = cast(list[str], raw_ids)
                if ids:
                    items = (await session.scalars(select(model).where(model.id.in_(ids)))).all()
                    by_id = {item.id: item.filename for item in items}
                    filenames.update(
                        (source_id, by_id[source_id]) for source_id in ids if source_id in by_id
                    )
        return filenames

    async def _answer_subquestion(
        self, query: str, evidence: list[Evidence], context: RunExecutionContext
    ) -> SubquestionAnswer:
        return await self._models.answer_subquestion(query, evidence, context)

    @staticmethod
    def _missing_facts(facts: list[str]) -> str:
        facts = [fact.strip() for fact in facts if fact.strip()]
        facts = list(dict.fromkeys(facts))[:3]
        return "；".join(facts)[:300] or "足以回答该问题的具体事实"

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

    async def _prepare(self, conversation_id: str, current_user_message_id: str) -> PreparedContext:
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
        self,
        conversation_id: str,
        prepared: PreparedContext,
        context: RunExecutionContext,
    ) -> PreparedContext:
        try:
            context.model_call_count += 1
            result = await self._chat.complete_text(
                summary_messages(prepared),
                timeout_seconds=context.timeout_seconds(),
            )
        except ChatError:
            return prepared
        context.record_usage("SUMMARY", result.usage)
        cursor = prepared.summary_candidates[-1].id
        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if conversation is None:
                raise RuntimeError("Accepted conversation no longer exists")
            conversation.summary_text = result.content
            conversation.summary_through_message_id = cursor
        return PreparedContext(
            summary_text=result.content,
            recent_messages=prepared.recent_messages,
            summary_candidates=prepared.summary_candidates,
        )

    async def _converge_success(
        self,
        *,
        conversation_id: str,
        run_id: str,
        user_message_id: str,
        assistant_message_id: str,
        context: RunExecutionContext,
    ) -> None:
        now = self._utc_now()
        async with self._sessions() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise RuntimeError("Answering terminal state is missing")
            trace_json = await self._diagnostics.terminal_trace(
                session, run, context, now, "SUCCEEDED"
            )
        await AnsweringFinalizer(self._sessions).commit_success(
            context.outcome(),
            conversation_id=conversation_id,
            run_id=run_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            finished_at=now,
            trace_json=trace_json,
            trace_version=self._policy.trace_version,
            policy_version=self._policy.policy_version,
        )
        if self._cleanup is not None:
            await self._cleanup.on_run_terminal(run_id)

    async def _converge_failure(
        self,
        run_id: str,
        assistant_message_id: str,
        context: RunExecutionContext,
        error_code: str,
    ) -> None:
        now = self._utc_now()
        async with self._sessions() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise RuntimeError("Answering failure state is missing")
            trace_json = await self._diagnostics.terminal_trace(
                session, run, context, now, error_code
            )
        await AnsweringFinalizer(self._sessions).commit_failure(
            context.outcome(),
            run_id=run_id,
            assistant_message_id=assistant_message_id,
            finished_at=now,
            trace_json=trace_json,
            trace_version=self._policy.trace_version,
            policy_version=self._policy.policy_version,
            error_code=error_code,
        )
        if self._cleanup is not None:
            await self._cleanup.on_run_terminal(run_id)

    def _schedule_interruption(
        self,
        run_id: str,
        assistant_message_id: str,
        context: RunExecutionContext,
    ) -> None:
        task = asyncio.create_task(
            self._converge_interruption(run_id, assistant_message_id, context)
        )
        self._settlement_tasks.add(task)
        task.add_done_callback(self._settlement_tasks.discard)

    async def _converge_interruption(
        self,
        run_id: str,
        assistant_message_id: str,
        context: RunExecutionContext,
    ) -> None:
        now = self._utc_now()
        async with self._sessions() as session, session.begin():
            run = await session.get(AgentRun, run_id, with_for_update=True)
            assistant_message = await session.get(
                Message, assistant_message_id, with_for_update=True
            )
            if run is None or assistant_message is None or run.status != RunStatus.RUNNING:
                return
            if context.current_stage_id is not None:
                stage = await session.get(AgentRunStage, context.current_stage_id)
                if stage is not None and stage.status == StageStatus.RUNNING:
                    stage.status = StageStatus.INTERRUPTED
                    stage.finished_at = now
            run.status = RunStatus.PARTIAL
            run.route = context.route
            run.error_code = None
            run.finished_at = now
            run.trace_version = self._policy.trace_version
            run.policy_version = self._policy.policy_version
            run.trace_json = await self._diagnostics.terminal_trace(
                session, run, context, now, "CLIENT_DISCONNECTED"
            )
            assistant_message.content = context.answer
            assistant_message.status = MessageStatus.PARTIAL
        if self._cleanup is not None:
            await self._cleanup.on_run_terminal(run_id)

    async def recover_interrupted_runs(self) -> None:
        now = self._utc_now()
        recovered_ids: list[str] = []
        async with self._sessions() as session, session.begin():
            runs = list(
                (
                    await session.scalars(
                        select(AgentRun)
                        .where(AgentRun.status == RunStatus.RUNNING)
                        .with_for_update()
                    )
                ).all()
            )
            for run in runs:
                assistant = await session.get(
                    Message, run.assistant_message_id, with_for_update=True
                )
                user_message = await session.get(Message, run.user_message_id)
                stages = list(
                    (
                        await session.scalars(
                            select(AgentRunStage)
                            .where(
                                AgentRunStage.run_id == run.id,
                                AgentRunStage.status == StageStatus.RUNNING,
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                for stage in stages:
                    stage.status = StageStatus.INTERRUPTED
                    stage.finished_at = now
                context = RunExecutionContext(self._policy, run.started_at)
                context.route = run.route
                run.status = RunStatus.FAILED
                run.error_code = "INTERNAL_ERROR"
                run.finished_at = now
                run.trace_version = self._policy.trace_version
                run.policy_version = self._policy.policy_version
                run.trace_json = await self._diagnostics.terminal_trace(
                    session, run, context, now, "STARTUP_RECOVERY"
                )
                if assistant is not None and assistant.status == MessageStatus.PENDING:
                    assistant.status = MessageStatus.PARTIAL
                    if user_message is not None and assistant.created_at <= user_message.created_at:
                        assistant.created_at = user_message.created_at + timedelta(microseconds=1)
                recovered_ids.append(run.id)
        if self._cleanup is not None:
            for run_id in recovered_ids:
                await self._cleanup.on_run_terminal(run_id)

    @staticmethod
    def _discard_unpublished_retrieval(context: RunExecutionContext) -> None:
        if context.defer_retrieval_publish:
            context.answer_parts.clear()
            context.citations.clear()

    def _append_visible_delta(self, context: RunExecutionContext, delta: str) -> tuple[str, bool]:
        remaining = self._policy.limits.max_visible_characters - len(context.answer)
        if remaining <= 0:
            return "", True
        if len(delta) <= remaining:
            context.answer_parts.append(delta)
            return delta, False
        visible = delta[:remaining]
        if visible:
            context.answer_parts.append(visible)
        return visible, True

    @staticmethod
    def _validated_content(content: object) -> str:
        if not isinstance(content, str):
            raise AppError(422, "VALIDATION_FAILED", "请求参数校验失败")
        normalized = content.strip()
        if not 1 <= len(normalized) <= 8000:
            raise AppError(422, "VALIDATION_FAILED", "请求参数校验失败")
        return normalized

    @staticmethod
    def _validated_client_request_id(value: object) -> str:
        if not isinstance(value, str):
            raise AppError(422, "VALIDATION_FAILED", "请求参数校验失败")
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise AppError(422, "VALIDATION_FAILED", "请求参数校验失败") from exc
        if parsed.version != 4:
            raise AppError(422, "VALIDATION_FAILED", "请求参数校验失败")
        return value

    @staticmethod
    def _failed_event(
        request_id: str,
        run_id: str,
        assistant_message_id: str,
        error_code: str,
        observation: dict[str, object] | None = None,
    ) -> RunFailedEvent:
        messages = {
            "MODEL_FAILED": "回答生成失败",
            "RUN_LIMIT_EXCEEDED": "回答超出执行限制",
            "INTERNAL_ERROR": "回答处理失败",
        }
        return RunFailedEvent(
            "run.failed",
            request_id,
            run_id,
            assistant_message_id,
            {"code": error_code, "message": messages.get(error_code, "回答处理失败")},
            observation=observation,
        )

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
