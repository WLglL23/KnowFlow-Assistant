from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.app.errors import AppError
from knowflow.identity.models import Account
from knowflow.knowledge.answering.chat import (
    ChatClient,
    ChatConfigurationError,
    ChatError,
    ChatMessage,
    ChatStreamUsage,
    ChatUsage,
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
from knowflow.knowledge.answering.evidence_budget import (
    EvidenceBudget,
    allocate_evidence,
    prioritize_candidates,
    promote_query_anchors,
)
from knowflow.knowledge.answering.finalizer import AnsweringFinalizer
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
from knowflow.knowledge.answering.observation_projection import SafeObservationProjector
from knowflow.knowledge.answering.outcomes import (
    AnsweringOutcome,
    LimitCompliance,
    SubquestionOutcome,
)
from knowflow.knowledge.answering.policy import RunPolicy
from knowflow.knowledge.answering.retrieval import (
    AttachmentRetrieval,
    Evidence,
    EvidenceTokenizerV1,
    RetrievalError,
)
from knowflow.knowledge.answering.subquestion_runner import (
    SubquestionAnswer,
    SubquestionRunner,
)
from knowflow.knowledge.answering.trace import (
    RunTraceV1,
    TraceRetry,
    TraceStageTiming,
    TraceTiming,
    TraceUsage,
    TraceUsageCall,
    TraceVersions,
)
from knowflow.knowledge.answering.trace_projection import (
    TraceProjectionMetadata,
    TraceProjector,
)
from knowflow.knowledge.conversations.models import Conversation
from knowflow.knowledge.conversations.service import allowed_source_scope
from knowflow.knowledge.documents.models import ConversationAttachment

if TYPE_CHECKING:
    from knowflow.knowledge.documents.cleanup import CleanupDocuments


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: AnswerRoute
    standalone_question: str
    reason_code: str


class RetrievalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[str]


class AnswerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ANSWERED", "INSUFFICIENT"]
    answer: str = ""
    labels: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)


class RunExecutionError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(slots=True)
class RunExecutionContext:
    policy: RunPolicy
    started_at: datetime
    answer_parts: list[str] = field(default_factory=lambda: [])
    usage_calls: list[TraceUsageCall] = field(default_factory=lambda: [])
    model_call_count: int = 0
    retries_used: int = 0
    route: AnswerRoute | None = None
    current_stage_id: str | None = None
    current_stage: StageName | None = None
    terminal_committed: bool = False
    evidence: list[Evidence] = field(default_factory=lambda: [])
    evidence_token_count: int = 0
    citations: list[dict[str, object]] = field(default_factory=lambda: [])
    subquestions: list[SubquestionOutcome] = field(default_factory=lambda: [])
    candidate_sets: list[list[Evidence]] = field(default_factory=lambda: [])
    actual_queries: int = 0
    duplicate_evidence_count: int = 0
    source_scope_compliant: bool | None = None
    defer_retrieval_publish: bool = False

    @property
    def answer(self) -> str:
        return "".join(self.answer_parts)

    def remaining_seconds(self, now: datetime) -> float:
        elapsed = max(0.0, (now - self.started_at).total_seconds())
        return max(0.0, self.policy.limits.deadline_seconds - elapsed)

    def consume_retry(self) -> bool:
        if self.retries_used >= self.policy.limits.max_retries:
            return False
        self.retries_used += 1
        return True

    def record_usage(self, operation: str, usage: ChatUsage | None) -> None:
        if usage is None:
            return
        self.usage_calls.append(
            TraceUsageCall(
                operation=operation,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            )
        )

    def outcome(self) -> AnsweringOutcome:
        retrieval_round_count = max(
            (item.retrieval_round_count for item in self.subquestions), default=0
        )
        budget_ok = (
            len(self.evidence) <= self.policy.limits.max_evidence_chunks
            and self.evidence_token_count <= self.policy.limits.max_evidence_tokens
            and all(
                len(item.evidence) <= self.policy.limits.max_subquestion_evidence_chunks
                and sum(EvidenceTokenizerV1.count(value.text) for value in item.evidence)
                <= self.policy.limits.max_subquestion_evidence_tokens
                for item in self.subquestions
            )
        )
        return AnsweringOutcome(
            run_status=(
                "PARTIAL" if any(item.failure_code for item in self.subquestions) else "SUCCEEDED"
            ),
            route=self.route.value if self.route is not None else "GENERAL",
            subquestions=list(self.subquestions),
            citations=list(self.citations),
            failure_code=None,
            limit_compliance=LimitCompliance(
                run=(
                    len(self.subquestions) <= self.policy.limits.max_subquestions
                    and retrieval_round_count <= self.policy.limits.max_retrieval_rounds
                    and self.actual_queries <= self.policy.limits.max_actual_queries
                ),
                visible_characters=len(self.answer) <= self.policy.limits.max_visible_characters,
                budget=budget_ok,
                source_scope=self.source_scope_compliant,
            ),
            answer=self.answer,
            query_count=self.actual_queries,
            retrieval_round_count=retrieval_round_count,
            duplicate_chunk_count=self.duplicate_evidence_count,
            source_type_count=len({item.source_type for item in self.evidence}),
            selected_evidence_token_count=self.evidence_token_count,
            model_call_count=self.model_call_count,
            latency_ms_total=max(
                0,
                round(
                    (datetime.now(UTC).replace(tzinfo=None) - self.started_at).total_seconds()
                    * 1000
                ),
            ),
        )


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
        self._policy = policy
        self._retrieval = retrieval
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
                    trace_json=self._trace_without_stages(
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
            await self._fail_current_stage(context)
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
                observation=self._safe_observation(context, "RUN_LIMIT_EXCEEDED"),
            )
        except RunExecutionError as exc:
            if context.terminal_committed:
                return
            self._discard_unpublished_retrieval(context)
            await self._fail_current_stage(context)
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
                observation=self._safe_observation(context, exc.error_code),
            )
        except (asyncio.CancelledError, GeneratorExit):
            if not context.terminal_committed:
                self._schedule_interruption(run_id, assistant_message_id, context)
            raise
        except Exception:
            if context.terminal_committed:
                return
            self._discard_unpublished_retrieval(context)
            await self._fail_current_stage(context)
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
                observation=self._safe_observation(context, "INTERNAL_ERROR"),
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
        await self._enter_stage(run_id, StageName.UNDERSTAND, context)
        yield StageEvent("stage.started", request_id, run_id, StageName.UNDERSTAND.value)

        prepared = await self._prepare(conversation_id, user_message_id)
        if prepared.summary_candidates:
            prepared = await self._summarize(conversation_id, prepared, context)
        try:
            decision = await self._route(prepared, content, context)
        except (ChatError, ValidationError) as exc:
            raise RunExecutionError("MODEL_FAILED") from exc
        context.route = decision.route
        await self._complete_understand_stage(context.current_stage_id, run_id, decision.route)
        context.current_stage_id = None
        context.current_stage = None
        yield StageEvent("stage.completed", request_id, run_id, StageName.UNDERSTAND.value)

        if decision.route is AnswerRoute.RETRIEVAL:
            source_filenames = await self._source_filenames(run_id)
            context.source_scope_compliant = True
            if source_filenames:
                context.defer_retrieval_publish = True
                await self._enter_stage(run_id, StageName.PLAN, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.PLAN.value)
                plan = await self._plan(prepared, decision, context)
                await self._complete_stage(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.PLAN.value)

                await self._enter_stage(run_id, StageName.RETRIEVE_1, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.RETRIEVE_1.value)
                runner = SubquestionRunner(
                    lambda query: self._retrieve_query(query, source_filenames, context),
                    lambda query, evidence: self._answer_subquestion(
                        prepared, query, evidence, context
                    ),
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
                                prioritize_candidates(query, retrieved.candidates),
                            )
                        )
                self._allocate_evidence(context)
                await self._complete_stage(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.RETRIEVE_1.value)

                await self._enter_stage(run_id, StageName.VERIFY, context)
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
                    elif not self._valid_evidence(state.evidence, source_filenames, context):
                        state.status = "FAILED"
                        state.failure_code = "SOURCE_SCOPE_VIOLATION"
                await self._complete_stage(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.VERIFY.value)

                await self._enter_stage(run_id, StageName.GENERATE, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.GENERATE.value)
                try:
                    context.evidence = self._unique_evidence(context.subquestions)
                    context.evidence_token_count = sum(
                        self._token_count(item) for item in context.evidence
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
                        item
                        for result in generated
                        if result is not None
                        for item in result[1]
                    ]
                    context.citations = self._citations(answer_evidence)
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
                                fragment + self._citation_markers(used_evidence, context.citations)
                            )
                        else:
                            missing = self._missing_facts(state.missing_facts)
                            fragments.append(f"未能确认相关内容；尚缺少：{missing}。")
                    answer = "\n\n".join(fragments)
                    visible, exceeded = self._append_visible_delta(context, answer)
                    pending_retrieval_delta = visible
                    if exceeded:
                        raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                    await self._complete_stage(context.current_stage_id)
                except ChatError as exc:
                    raise RunExecutionError("MODEL_FAILED") from exc
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.GENERATE.value)
            else:
                context.defer_retrieval_publish = True
                await self._enter_stage(run_id, StageName.GENERATE, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.GENERATE.value)
                refusal = "当前没有可用资料。请上传附件或开启并选择知识源后再试。"
                visible, exceeded = self._append_visible_delta(context, refusal)
                pending_retrieval_delta = visible
                if exceeded:
                    raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                await self._complete_stage(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.GENERATE.value)
        else:
            await self._enter_stage(run_id, StageName.GENERATE, context)
            yield StageEvent("stage.started", request_id, run_id, StageName.GENERATE.value)
            try:
                async for delta in self._generate(prepared, content, decision, context):
                    visible, exceeded = self._append_visible_delta(context, delta)
                    if visible:
                        yield AnswerDeltaEvent("answer.delta", request_id, run_id, visible)
                    if exceeded:
                        raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                await self._complete_stage(context.current_stage_id)
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
                observation=self._safe_observation(context, "INTERNAL_ERROR"),
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
            observation=self._safe_observation(context, None),
        )

    async def _source_filenames(self, run_id: str) -> dict[str, str]:
        async with self._sessions() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise RuntimeError("Answering run is missing")
            raw_ids = run.source_scope_json.get("attachment_ids")
            if not isinstance(raw_ids, list):
                raise RuntimeError("Answering run source scope is invalid")
            id_values = cast(list[object], raw_ids)
            if not all(isinstance(item, str) for item in id_values):
                raise RuntimeError("Answering run source scope is invalid")
            ids = cast(list[str], id_values)
            if not ids:
                return {}
            items = list(
                (
                    await session.scalars(
                        select(ConversationAttachment).where(ConversationAttachment.id.in_(ids))
                    )
                ).all()
            )
        return {item.id: item.filename for item in items if item.id in ids}

    async def _plan(
        self, prepared: PreparedContext, decision: RouteDecision, context: RunExecutionContext
    ) -> RetrievalPlan:
        messages = [
            ChatMessage(
                "system",
                "简单问题只生成一个独立检索查询；仅在用户明确包含多个意图时按原顺序拆分，"
                '最多五个。只返回 JSON：{"queries":["..."]}。',
            ),
            *prepared.chat_messages(decision.standalone_question),
        ]
        while True:
            try:
                context.model_call_count += 1
                result = await self._chat.complete_json(
                    messages, timeout_seconds=self._chat_timeout(context)
                )
                context.record_usage("PLAN", result.usage)
                plan = RetrievalPlan.model_validate(result.payload)
                queries = [query.strip() for query in plan.queries if query.strip()]
                if not queries:
                    raise ValidationError.from_exception_data("RetrievalPlan", [])
                return RetrievalPlan(queries=queries[: context.policy.limits.max_subquestions])
            except (ValidationError, ChatError) as exc:
                retryable = isinstance(exc, ValidationError) or exc.retryable
                if retryable and context.consume_retry():
                    continue
                raise RunExecutionError("MODEL_FAILED") from exc

    async def _retrieve_query(
        self,
        query: str,
        source_filenames: dict[str, str],
        context: RunExecutionContext,
    ) -> tuple[list[Evidence], int, int, list[str]]:
        if context.actual_queries >= context.policy.limits.max_actual_queries:
            raise RunExecutionError("RUN_LIMIT_EXCEEDED")
        context.actual_queries += 1
        while True:
            try:
                vector = await self._retrieval.embed(
                    query, timeout_seconds=self._chat_timeout(context)
                )
                break
            except RetrievalError as exc:
                if not (exc.retryable and context.consume_retry()):
                    raise RunExecutionError("RETRIEVAL_FAILED") from exc
        try:
            evidence = await self._retrieval.hybrid(
                query, vector, source_filenames, timeout_seconds=self._chat_timeout(context)
            )
        except RetrievalError as exc:
            raise RunExecutionError("RETRIEVAL_FAILED") from exc
        while True:
            try:
                initial_page_hashes = list(
                    dict.fromkeys(
                        hashlib.sha256(f"{item.source_id}:{page}".encode()).hexdigest()
                        for item in evidence
                        for page in item.pages
                    )
                )
                reranked = await self._retrieval.rerank(
                    query, evidence, timeout_seconds=self._chat_timeout(context)
                )
                return reranked, len(evidence), len(reranked), initial_page_hashes
            except RetrievalError as exc:
                if not (exc.retryable and context.consume_retry()):
                    raise RunExecutionError("RETRIEVAL_FAILED") from exc

    async def _answer_subquestion(
        self,
        prepared: PreparedContext,
        query: str,
        evidence: list[Evidence],
        context: RunExecutionContext,
    ) -> SubquestionAnswer:
        evidence_text = "\n\n".join(
            f"E{index + 1} {item.filename} 第 {','.join(map(str, item.pages))} 页：{item.text}"
            for index, item in enumerate(evidence)
        )
        messages = [
            ChatMessage(
                "system",
                "只根据给定 PDF 证据判断并回答，不补充通用知识，不输出引用标记。"
                '只返回 JSON：{"status":"ANSWERED|INSUFFICIENT",'
                '"answer":"...","labels":["E1"],"missing_facts":["..."]}。'
                "证据不足时 status=INSUFFICIENT、answer 为空、labels 为空。",
            ),
            *prepared.chat_messages(query),
            ChatMessage("system", f"独立问题：{query}\n证据：\n{evidence_text}"),
        ]
        try:
            context.model_call_count += 1
            result = await self._chat.complete_json(
                messages, timeout_seconds=self._chat_timeout(context)
            )
            context.record_usage("ANSWER", result.usage)
            validated = AnswerDecision.model_validate(result.payload)
            return SubquestionAnswer(
                validated.status,
                validated.answer,
                validated.labels,
                validated.missing_facts,
            )
        except (ValidationError, ChatError) as exc:
            raise RunExecutionError("MODEL_FAILED") from exc

    @staticmethod
    def _token_count(item: Evidence) -> int:
        from knowflow.knowledge.answering.retrieval import EvidenceTokenizerV1

        return EvidenceTokenizerV1.count(item.text)

    def _allocate_evidence(self, context: RunExecutionContext) -> None:
        limits = self._policy.limits
        allocation = allocate_evidence(
            context.candidate_sets,
            EvidenceBudget(
                global_chunks=limits.max_evidence_chunks,
                global_tokens=limits.max_evidence_tokens,
                per_subquestion_chunks=limits.max_subquestion_evidence_chunks,
                per_subquestion_tokens=limits.max_subquestion_evidence_tokens,
            ),
        )
        for state, evidence in zip(context.subquestions, allocation.by_subquestion, strict=True):
            state.evidence = evidence
            state.selected_evidence_token_count = sum(
                EvidenceTokenizerV1.count(item.text) for item in evidence
            )
        context.evidence = allocation.unique_evidence
        context.evidence_token_count = allocation.total_tokens
        context.duplicate_evidence_count = allocation.duplicate_chunk_count

    @staticmethod
    def _unique_evidence(states: list[SubquestionOutcome]) -> list[Evidence]:
        result: list[Evidence] = []
        seen: set[tuple[str, str]] = set()
        for state in states:
            for item in state.evidence:
                key = (item.source_id, item.chunk_id)
                if key not in seen:
                    seen.add(key)
                    result.append(item)
        return result

    @staticmethod
    def _missing_facts(facts: list[str]) -> str:
        facts = [fact.strip() for fact in facts if fact.strip()]
        facts = list(dict.fromkeys(facts))[:3]
        return "；".join(facts)[:300] or "足以回答该问题的具体事实"

    @staticmethod
    def _valid_evidence(
        evidence: list[Evidence], source_filenames: dict[str, str], context: RunExecutionContext
    ) -> bool:
        if any(
            not item.text.strip()
            or item.source_id not in source_filenames
            or not item.pages
            for item in evidence
        ):
            context.source_scope_compliant = False
            return False
        return True

    @staticmethod
    def _citation_markers(evidence: list[Evidence], citations: list[dict[str, object]]) -> str:
        indexes: list[str] = []
        for item in evidence:
            for index, citation in enumerate(citations):
                if citation.get("source_id") == item.source_id:
                    indexes.append(str(index))
                    break
        return "".join(f"[[citation:{index}]]" for index in dict.fromkeys(indexes))

    @staticmethod
    def _citations(evidence: list[Evidence]) -> list[dict[str, object]]:
        citations: list[dict[str, object]] = []
        for item in evidence:
            current = next(
                (citation for citation in citations if citation["source_id"] == item.source_id),
                None,
            )
            if current is None:
                citations.append(
                    {
                        "source_type": item.source_type,
                        "source_id": item.source_id,
                        "filename": item.filename,
                        "pages": list(item.pages[:5]),
                    }
                )
            else:
                raw_pages = current.get("pages")
                pages = (
                    [page for page in cast(list[object], raw_pages) if isinstance(page, int)]
                    if isinstance(raw_pages, list)
                    else []
                )
                current["pages"] = list(dict.fromkeys(pages + list(item.pages)))[:5]
        return citations

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
                timeout_seconds=self._chat_timeout(context),
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

    async def _route(
        self,
        prepared: PreparedContext,
        content: str,
        context: RunExecutionContext,
    ) -> RouteDecision:
        messages = [
            ChatMessage(
                "system",
                "判断问题应走 GENERAL 还是 RETRIEVAL。只返回 JSON："
                '{"route":"GENERAL|RETRIEVAL","standalone_question":"...",'
                '"reason_code":"..."}。明确要求 PDF 或资料证据时选择 RETRIEVAL。',
            ),
            *prepared.chat_messages(content),
        ]
        while True:
            try:
                context.model_call_count += 1
                result = await self._chat.complete_json(
                    messages,
                    timeout_seconds=self._chat_timeout(context),
                )
                context.record_usage("UNDERSTAND", result.usage)
                return RouteDecision.model_validate(result.payload)
            except ValidationError:
                if context.consume_retry():
                    continue
                raise
            except ChatError as exc:
                if exc.retryable and context.consume_retry():
                    continue
                raise

    async def _generate(
        self,
        prepared: PreparedContext,
        content: str,
        decision: RouteDecision,
        context: RunExecutionContext,
    ) -> AsyncIterator[str]:
        messages = [
            ChatMessage("system", "直接回答用户问题。不要声称检索或引用任何 PDF。"),
            *prepared.chat_messages(content),
            ChatMessage("system", f"独立问题：{decision.standalone_question}"),
        ]
        emitted = False
        while True:
            try:
                context.model_call_count += 1
                async for event in self._chat.stream_text(
                    messages,
                    timeout_seconds=self._chat_timeout(context),
                ):
                    if isinstance(event, ChatStreamUsage):
                        context.record_usage("GENERATE", event.usage)
                    else:
                        emitted = True
                        yield event.delta
                return
            except ChatError as exc:
                if not emitted and exc.retryable and context.consume_retry():
                    continue
                raise

    async def _enter_stage(
        self, run_id: str, stage: StageName, context: RunExecutionContext
    ) -> None:
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
        context.current_stage_id = stage_id
        context.current_stage = stage

    async def _complete_understand_stage(
        self, stage_id: str | None, run_id: str, route: AnswerRoute
    ) -> None:
        if stage_id is None:
            raise RuntimeError("Answering stage is missing")
        async with self._sessions() as session, session.begin():
            stage = await session.get(AgentRunStage, stage_id)
            run = await session.get(AgentRun, run_id)
            if stage is None or run is None:
                raise RuntimeError("Answering state is missing")
            stage.status = StageStatus.SUCCEEDED
            stage.finished_at = self._utc_now()
            run.route = route

    async def _complete_stage(self, stage_id: str | None) -> None:
        if stage_id is None:
            raise RuntimeError("Answering stage is missing")
        async with self._sessions() as session, session.begin():
            stage = await session.get(AgentRunStage, stage_id)
            if stage is None:
                raise RuntimeError("Answering stage is missing")
            stage.status = StageStatus.SUCCEEDED
            stage.finished_at = self._utc_now()

    async def _fail_current_stage(self, context: RunExecutionContext) -> None:
        if context.current_stage_id is None:
            return
        async with self._sessions() as session, session.begin():
            stage = await session.get(AgentRunStage, context.current_stage_id)
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
        context: RunExecutionContext,
    ) -> None:
        now = self._utc_now()
        async with self._sessions() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise RuntimeError("Answering terminal state is missing")
            trace_json = await self._terminal_trace(session, run, context, now, "SUCCEEDED")
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
            trace_json = await self._terminal_trace(session, run, context, now, error_code)
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
            run.trace_json = await self._terminal_trace(
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
                run.trace_json = await self._terminal_trace(
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

    async def _terminal_trace(
        self,
        session: AsyncSession,
        run: AgentRun,
        context: RunExecutionContext,
        finished_at: datetime,
        termination: str,
    ) -> dict[str, object]:
        stages = list(
            (
                await session.scalars(
                    select(AgentRunStage)
                    .where(AgentRunStage.run_id == run.id)
                    .order_by(AgentRunStage.started_at.asc(), AgentRunStage.id.asc())
                )
            ).all()
        )
        stage_timings = [
            TraceStageTiming(
                stage=stage.stage.value,
                duration_ms=self._duration_ms(stage.started_at, stage.finished_at or finished_at),
            )
            for stage in stages
        ]
        trace = self._trace_model(
            context,
            total_ms=self._duration_ms(run.started_at, finished_at),
            stage_timings=stage_timings,
            termination=termination,
        )
        return trace.as_json()

    def _trace_without_stages(
        self,
        context: RunExecutionContext,
        *,
        total_ms: int,
        termination: str | None,
    ) -> dict[str, object]:
        return self._trace_model(
            context,
            total_ms=total_ms,
            stage_timings=[],
            termination=termination,
        ).as_json()

    def _trace_model(
        self,
        context: RunExecutionContext,
        *,
        total_ms: int,
        stage_timings: list[TraceStageTiming],
        termination: str | None,
    ) -> RunTraceV1:
        calls = context.usage_calls
        return TraceProjector().project(
            context.outcome(),
            TraceProjectionMetadata(
                versions=TraceVersions(
                    trace_version=self._policy.trace_version,
                    policy_version=self._policy.policy_version,
                ),
                usage=TraceUsage(
                    calls=calls,
                    request_count=context.model_call_count,
                    prompt_tokens=sum(call.prompt_tokens for call in calls),
                    completion_tokens=sum(call.completion_tokens for call in calls),
                    total_tokens=sum(call.total_tokens for call in calls),
                ),
                timing=TraceTiming(total_ms=total_ms, stages=stage_timings),
                retry=TraceRetry(
                    limit=self._policy.limits.max_retries,
                    used=context.retries_used,
                ),
                termination=termination,
            ),
        )

    def _safe_observation(
        self, context: RunExecutionContext, failure_code: str | None
    ) -> dict[str, object]:
        return SafeObservationProjector().project(
            context.outcome(), failure_code=failure_code
        )

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

    def _chat_timeout(self, context: RunExecutionContext) -> float:
        remaining = context.remaining_seconds(self._utc_now())
        if remaining <= 0:
            raise RunExecutionError("RUN_LIMIT_EXCEEDED")
        return remaining

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
    def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
        return max(0, round((finished_at - started_at).total_seconds() * 1000))

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
