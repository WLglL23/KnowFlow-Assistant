from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError
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
from knowflow.knowledge.answering.grounding import (
    GROUNDING_CONTROL_PREFIX,
    GROUNDING_CONTROL_SUFFIX,
    GroundedAnswerParser,
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
from knowflow.knowledge.answering.policy import RunPolicy
from knowflow.knowledge.answering.retrieval import AttachmentRetrieval, Evidence, RetrievalError
from knowflow.knowledge.answering.trace import (
    RunTraceV1,
    TraceRetrieval,
    TraceRetry,
    TraceStageTiming,
    TraceTermination,
    TraceTiming,
    TraceUsage,
    TraceUsageCall,
    TraceVersions,
)
from knowflow.knowledge.conversations.models import Conversation
from knowflow.knowledge.conversations.service import allowed_source_scope
from knowflow.knowledge.documents.models import ConversationAttachment, KnowledgeSubmission


class UnderstandingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    standalone_question: str


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: AnswerRoute
    standalone_question: str


class PlannedSubquestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str


class RetrievalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subquestions: list[PlannedSubquestion]


class SubquestionVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subquestion_id: str
    sufficiency: Literal["SUFFICIENT", "INSUFFICIENT"]
    labels: list[str]
    missing_facts: list[str]
    supplemental_query: str | None


class VerificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[SubquestionVerification]


@dataclass(frozen=True, slots=True)
class RetrievalEvidence:
    by_label: dict[str, Evidence]
    labels_by_subquestion: dict[str, tuple[str, ...]]


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
    retries_used: int = 0
    route: AnswerRoute | None = None
    coverage: str | None = None
    current_stage_id: str | None = None
    current_stage: StageName | None = None
    terminal_committed: bool = False
    evidence: list[Evidence] = field(default_factory=lambda: [])
    evidence_token_count: int = 0
    citations: list[dict[str, object]] = field(default_factory=lambda: [])
    retrieval_round_count: int = 0
    actual_query_count: int = 0
    planned_subquestion_count: int = 0
    sufficient_subquestion_count: int = 0
    missing_fact_count: int = 0

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


class AnsweringService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        chat: ChatClient,
        policy: RunPolicy,
        retrieval: AttachmentRetrieval,
    ) -> None:
        self._sessions = sessions
        self._chat = chat
        self._policy = policy
        self._retrieval = retrieval
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
            await self._fail_current_stage(context)
            await self._converge_failure(
                run_id,
                assistant_message_id,
                context,
                "RUN_LIMIT_EXCEEDED",
            )
            context.terminal_committed = True
            yield self._failed_event(request_id, run_id, assistant_message_id, "RUN_LIMIT_EXCEEDED")
        except RunExecutionError as exc:
            if context.terminal_committed:
                return
            await self._fail_current_stage(context)
            await self._converge_failure(
                run_id,
                assistant_message_id,
                context,
                exc.error_code,
            )
            context.terminal_committed = True
            yield self._failed_event(request_id, run_id, assistant_message_id, exc.error_code)
        except (asyncio.CancelledError, GeneratorExit):
            if not context.terminal_committed:
                self._schedule_interruption(run_id, assistant_message_id, context)
            raise
        except Exception:
            if context.terminal_committed:
                return
            await self._fail_current_stage(context)
            await self._converge_failure(
                run_id,
                assistant_message_id,
                context,
                "INTERNAL_ERROR",
            )
            context.terminal_committed = True
            yield self._failed_event(request_id, run_id, assistant_message_id, "INTERNAL_ERROR")

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
        await self._enter_stage(run_id, StageName.UNDERSTAND, context)
        yield StageEvent("stage.started", request_id, run_id, StageName.UNDERSTAND.value)

        prepared = await self._prepare(conversation_id, user_message_id)
        if prepared.summary_candidates:
            prepared = await self._summarize(conversation_id, prepared, context)
        source_scope, source_filenames = await self._frozen_sources(run_id)
        route = self._deterministic_route(source_scope, content)
        try:
            understanding = await self._understand(prepared, content, context)
        except (ChatError, ValidationError) as exc:
            raise RunExecutionError("MODEL_FAILED") from exc
        decision = RouteDecision(route, understanding.standalone_question)
        context.route = decision.route
        await self._complete_understand_stage(context.current_stage_id, run_id, decision.route)
        context.current_stage_id = None
        context.current_stage = None
        yield StageEvent("stage.completed", request_id, run_id, StageName.UNDERSTAND.value)

        if decision.route is AnswerRoute.RETRIEVAL:
            if source_filenames:
                await self._enter_stage(run_id, StageName.PLAN, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.PLAN.value)
                plan = await self._plan(prepared, decision, context)
                context.planned_subquestion_count = len(plan.subquestions)
                await self._complete_stage(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.PLAN.value)

                await self._enter_stage(run_id, StageName.RETRIEVE_1, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.RETRIEVE_1.value)
                retrieval = await self._retrieve(
                    [(item.id, item.question) for item in plan.subquestions],
                    source_filenames,
                    context,
                )
                await self._complete_stage(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.RETRIEVE_1.value)

                await self._enter_stage(run_id, StageName.VERIFY, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.VERIFY.value)
                verification = await self._verify(plan, retrieval, context)
                await self._complete_stage(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.VERIFY.value)

                gap_queries = self._gap_queries(verification)
                if gap_queries:
                    first_round_evidence = retrieval
                    await self._enter_stage(run_id, StageName.RETRIEVE_2, context)
                    yield StageEvent(
                        "stage.started", request_id, run_id, StageName.RETRIEVE_2.value
                    )
                    retrieval = await self._retrieve(
                        gap_queries,
                        source_filenames,
                        context,
                        existing=retrieval,
                    )
                    await self._complete_stage(context.current_stage_id)
                    context.current_stage_id = None
                    context.current_stage = None
                    yield StageEvent(
                        "stage.completed", request_id, run_id, StageName.RETRIEVE_2.value
                    )

                    changed_ids = self._subquestions_with_new_evidence(
                        gap_queries, first_round_evidence, retrieval
                    )
                    if changed_ids:
                        gap_plan = RetrievalPlan(
                            subquestions=[
                                item
                                for item in plan.subquestions
                                if item.id in changed_ids
                            ]
                        )
                        await self._enter_stage(run_id, StageName.VERIFY, context)
                        yield StageEvent(
                            "stage.started", request_id, run_id, StageName.VERIFY.value
                        )
                        gap_verification = await self._verify(
                            gap_plan,
                            retrieval,
                            context,
                            required_missing_facts={
                                item.subquestion_id: item.missing_facts
                                for item in verification
                                if item.subquestion_id in changed_ids
                            },
                        )
                        verification = self._merge_verification(
                            plan, verification, gap_verification
                        )
                        await self._complete_stage(context.current_stage_id)
                        context.current_stage_id = None
                        context.current_stage = None
                        yield StageEvent(
                            "stage.completed", request_id, run_id, StageName.VERIFY.value
                        )

                context.coverage = self._coverage(verification)
                context.sufficient_subquestion_count = sum(
                    item.sufficiency == "SUFFICIENT" for item in verification
                )
                context.missing_fact_count = sum(
                    len(item.missing_facts) for item in verification
                )
                answer_evidence = self._verified_evidence(
                    plan, verification, retrieval
                )
                context.evidence = []
                context.evidence_token_count = 0
                context.citations = []

                await self._enter_stage(run_id, StageName.GENERATE, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.GENERATE.value)
                try:
                    if context.coverage in {"FULL", "PARTIAL"}:
                        async for delta in self._generate_grounded(
                            prepared,
                            content,
                            decision,
                            self._answered_subquestions(plan, verification),
                            answer_evidence,
                            context,
                        ):
                            visible, exceeded = self._append_visible_delta(context, delta)
                            if visible:
                                yield AnswerDeltaEvent("answer.delta", request_id, run_id, visible)
                            if exceeded:
                                raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                        if context.coverage == "PARTIAL":
                            notice = self._partial_notice(plan, verification)
                            visible, exceeded = self._append_visible_delta(context, notice)
                            if visible:
                                yield AnswerDeltaEvent(
                                    "answer.delta", request_id, run_id, visible
                                )
                            if exceeded:
                                raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                    else:
                        refusal = self._refusal(plan, verification)
                        visible, exceeded = self._append_visible_delta(context, refusal)
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
            else:
                await self._enter_stage(run_id, StageName.GENERATE, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.GENERATE.value)
                context.coverage = "NONE"
                refusal = "当前没有可用资料。请上传附件或开启并选择知识源后再试。"
                visible, exceeded = self._append_visible_delta(context, refusal)
                if visible:
                    yield AnswerDeltaEvent("answer.delta", request_id, run_id, visible)
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
            await self._converge_failure(
                run_id,
                assistant_message_id,
                context,
                "INTERNAL_ERROR",
            )
            context.terminal_committed = True
            yield self._failed_event(request_id, run_id, assistant_message_id, "INTERNAL_ERROR")
            return
        context.terminal_committed = True
        yield RunCompletedEvent("run.completed", request_id, run_id, assistant_message_id)

    async def _frozen_sources(
        self, run_id: str
    ) -> tuple[dict[str, object], dict[str, str]]:
        async with self._sessions() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise RuntimeError("Answering run is missing")
            source_scope = run.source_scope_json
            attachment_ids = self._scope_ids(source_scope, "attachment_ids")
            knowledge_ids = self._scope_ids(
                source_scope, "knowledge_source_ids"
            )
            attachments = list(
                (
                    await session.scalars(
                        select(ConversationAttachment).where(
                            ConversationAttachment.id.in_(attachment_ids)
                        )
                    )
                ).all()
            )
            knowledge_sources = list(
                (
                    await session.scalars(
                        select(KnowledgeSubmission).where(
                            KnowledgeSubmission.id.in_(knowledge_ids)
                        )
                    )
                ).all()
            )
        filenames = {
            item.id: item.filename for item in attachments if item.id in attachment_ids
        }
        filenames.update(
            {
                item.id: item.filename
                for item in knowledge_sources
                if item.id in knowledge_ids
            }
        )
        return source_scope, filenames

    @staticmethod
    def _scope_ids(scope: dict[str, object], name: str) -> list[str]:
        raw_ids = scope.get(name)
        if not isinstance(raw_ids, list):
            raise RuntimeError("Answering run source scope is invalid")
        id_values = cast(list[object], raw_ids)
        if not all(isinstance(item, str) for item in id_values):
            raise RuntimeError("Answering run source scope is invalid")
        return cast(list[str], id_values)

    async def _plan(
        self, prepared: PreparedContext, decision: RouteDecision, context: RunExecutionContext
    ) -> RetrievalPlan:
        messages = [
            ChatMessage(
                "system",
                "把独立问题拆成一到五个可分别用 PDF 证据核验的子问题。"
                "id 必须在本次响应内唯一且后续保持不变，question 必须可独立检索。"
                "只返回严格 JSON 对象："
                '{"subquestions":[{"id":"sq_1","question":"..."}]}。'
                "不得返回 query 列表、coverage、解释或额外字段。",
            ),
            *prepared.chat_messages(decision.standalone_question),
        ]
        correction = ChatMessage(
            "user",
            "上一响应不符合 PLAN JSON contract。请重新返回唯一 subquestion id 和非空 "
            "question；只返回 subquestions 对象。",
        )
        while True:
            try:
                result = await self._chat.complete_json(
                    messages,
                    timeout_seconds=self._chat_timeout(context),
                )
                context.record_usage("PLAN", result.usage)
                plan = RetrievalPlan.model_validate(result.payload)
                normalized = [
                    PlannedSubquestion(id=item.id.strip(), question=item.question.strip())
                    for item in plan.subquestions
                ]
                if not normalized or any(
                    not item.id or not item.question for item in normalized
                ):
                    raise ValueError("PLAN contains an empty subquestion")
                ids = [item.id for item in normalized]
                if len(ids) != len(set(ids)):
                    raise ValueError("PLAN contains duplicate subquestion ids")
                if len(normalized) > self._policy.limits.max_subquestions:
                    raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                return RetrievalPlan(subquestions=normalized)
            except RunExecutionError:
                raise
            except (ValidationError, ValueError, ChatError) as exc:
                retryable = isinstance(exc, (ValidationError, ValueError)) or exc.retryable
                if retryable and context.consume_retry():
                    messages.append(correction)
                    continue
                raise RunExecutionError("MODEL_FAILED") from exc

    async def _retrieve(
        self,
        queries: list[tuple[str, str]],
        source_filenames: dict[str, str],
        context: RunExecutionContext,
        *,
        existing: RetrievalEvidence | None = None,
    ) -> RetrievalEvidence:
        if not queries:
            raise RuntimeError("Retrieval requires at least one query")
        limits = self._policy.limits
        if context.retrieval_round_count >= limits.max_retrieval_rounds:
            raise RunExecutionError("RUN_LIMIT_EXCEEDED")
        if context.actual_query_count + len(queries) > limits.max_actual_queries:
            raise RunExecutionError("RUN_LIMIT_EXCEEDED")
        context.retrieval_round_count += 1
        context.actual_query_count += len(queries)

        query_texts = [query for _subquestion_id, query in queries]
        while True:
            try:
                vectors = await self._retrieval.embed_many(
                    query_texts, timeout_seconds=self._chat_timeout(context)
                )
                break
            except RetrievalError as exc:
                if not (exc.retryable and context.consume_retry()):
                    raise RunExecutionError("RETRIEVAL_FAILED") from exc

        evidence_by_query: dict[str, list[Evidence]] = {}
        try:
            for (subquestion_id, query), vector in zip(queries, vectors, strict=True):
                evidence_by_query[subquestion_id] = await self._retrieval.hybrid(
                    query,
                    vector,
                    source_filenames,
                    timeout_seconds=self._chat_timeout(context),
                )
        except RetrievalError as exc:
            raise RunExecutionError("RETRIEVAL_FAILED") from exc

        round_evidence: list[Evidence] = []
        seen_chunks: set[tuple[str, str]] = set()
        for subquestion_id, _query in queries:
            for item in evidence_by_query[subquestion_id]:
                key = (item.source_id, item.chunk_id)
                if key not in seen_chunks:
                    seen_chunks.add(key)
                    round_evidence.append(item)
        while True:
            try:
                reranked = await self._retrieval.rerank(
                    "；".join(query_texts),
                    round_evidence,
                    timeout_seconds=self._chat_timeout(context),
                )
                break
            except RetrievalError as exc:
                if not (exc.retryable and context.consume_retry()):
                    raise RunExecutionError("RETRIEVAL_FAILED") from exc

        by_label = {} if existing is None else dict(existing.by_label)
        labels_by_subquestion = (
            {} if existing is None else dict(existing.labels_by_subquestion)
        )
        label_by_chunk = {
            (item.source_id, item.chunk_id): label for label, item in by_label.items()
        }
        rank = {
            (item.source_id, item.chunk_id): index for index, item in enumerate(reranked)
        }
        next_label = len(by_label) + 1
        for subquestion_id, _query in queries:
            labels = list(labels_by_subquestion.get(subquestion_id, ()))
            candidates = sorted(
                evidence_by_query[subquestion_id],
                key=lambda item: rank.get((item.source_id, item.chunk_id), len(rank)),
            )
            for item in candidates:
                key = (item.source_id, item.chunk_id)
                label = label_by_chunk.get(key)
                if label is None:
                    label = f"E{next_label}"
                    next_label += 1
                    label_by_chunk[key] = label
                    by_label[label] = item
                if label not in labels:
                    labels.append(label)
            labels_by_subquestion[subquestion_id] = tuple(labels)

        ordered_labels = self._coverage_first_labels(labels_by_subquestion)
        selected, _token_count = self._retrieval.select(
            [by_label[label] for label in ordered_labels],
            max_chunks=limits.max_evidence_chunks,
            max_tokens=limits.max_evidence_tokens,
        )
        selected_keys = {(item.source_id, item.chunk_id) for item in selected}
        selected_labels = {
            label
            for label, item in by_label.items()
            if (item.source_id, item.chunk_id) in selected_keys
        }
        return RetrievalEvidence(
            by_label={
                label: by_label[label]
                for label in ordered_labels
                if label in selected_labels
            },
            labels_by_subquestion={
                subquestion_id: tuple(
                    label for label in labels if label in selected_labels
                )
                for subquestion_id, labels in labels_by_subquestion.items()
            },
        )

    @staticmethod
    def _coverage_first_labels(
        labels_by_subquestion: dict[str, tuple[str, ...]],
    ) -> list[str]:
        ordered: list[str] = []
        depth = 0
        while any(depth < len(labels) for labels in labels_by_subquestion.values()):
            for labels in labels_by_subquestion.values():
                if depth < len(labels) and labels[depth] not in ordered:
                    ordered.append(labels[depth])
            depth += 1
        return ordered

    async def _verify(
        self,
        plan: RetrievalPlan,
        evidence: RetrievalEvidence,
        context: RunExecutionContext,
        *,
        required_missing_facts: dict[str, list[str]] | None = None,
    ) -> list[SubquestionVerification]:
        normalized = await self._verify_once(
            plan,
            evidence,
            context,
            required_missing_facts=required_missing_facts,
        )
        if required_missing_facts is not None:
            return normalized

        # A batch response can omit an otherwise available candidate for one
        # subquestion. Recheck only that ambiguous shape independently; a
        # negative independent result leaves the conservative batch decision in place.
        by_id = {item.subquestion_id: item for item in normalized}
        review_ids = [
            item.subquestion_id
            for item in normalized
            if item.sufficiency == "INSUFFICIENT"
            and not item.labels
            and evidence.labels_by_subquestion.get(item.subquestion_id)
        ]
        if (
            len(normalized) < 2
            or len(review_ids) != 1
            or sum(item.sufficiency == "SUFFICIENT" for item in normalized)
            != len(normalized) - 1
        ):
            return normalized
        plan_by_id = {item.id: item for item in plan.subquestions}
        for subquestion_id in review_ids:
            reviewed = await self._verify_once(
                RetrievalPlan(subquestions=[plan_by_id[subquestion_id]]),
                evidence,
                context,
                required_missing_facts={
                    subquestion_id: by_id[subquestion_id].missing_facts
                },
                independent=True,
            )
            if reviewed[0].sufficiency == "SUFFICIENT":
                by_id[subquestion_id] = reviewed[0]
        return [by_id[item.id] for item in plan.subquestions]

    async def _verify_once(
        self,
        plan: RetrievalPlan,
        evidence: RetrievalEvidence,
        context: RunExecutionContext,
        *,
        required_missing_facts: dict[str, list[str]] | None = None,
        independent: bool = False,
    ) -> list[SubquestionVerification]:
        evidence_sections: list[str] = []
        for subquestion in plan.subquestions:
            labels = evidence.labels_by_subquestion.get(subquestion.id, ())
            rendered = "\n".join(
                f"[{label}] 来源：{evidence.by_label[label].filename} "
                f"页码：{','.join(map(str, evidence.by_label[label].pages))}\n"
                f"{evidence.by_label[label].text}"
                for label in labels
            )
            evidence_sections.append(
                f"子问题 {subquestion.id}：{subquestion.question}\n"
                + (
                    "本轮必须补足的缺失事实："
                    + "；".join(required_missing_facts[subquestion.id])
                    + "\n"
                    if required_missing_facts
                    and subquestion.id in required_missing_facts
                    else ""
                )
                + f"候选证据：\n{rendered or '无'}"
            )
        messages = [
            ChatMessage(
                "system",
                "逐题核验候选 PDF 证据。每个 PLAN 子问题必须恰好返回一个结果，"
                "subquestion_id 必须原样复用。sufficiency 只能是 SUFFICIENT 或 "
                "INSUFFICIENT；labels 只能引用该题列出的证据标签；SUFFICIENT 必须"
                "至少有一个 label 且 missing_facts 为空；INSUFFICIENT 必须明确列出"
                "missing_facts。每题 supplemental_query 只能是一个非空字符串或 null。"
                "充分性表示证据直接支持该子问题要求的全部事实和全部限定条件；只有主题、"
                "关键词或文档相关不构成充分。不得用模型通用知识、推断或常识补齐证据。"
                "多条件问题缺少任一条件都必须返回 INSUFFICIENT。第二轮只有新增证据直接"
                "补足列出的全部缺失事实时才可改为 SUFFICIENT。"
                + (
                    "这是对批量核验的单题独立复核；只判断当前子问题，不要因其他子问题的"
                    "结果改变当前判断。"
                    if independent
                    else ""
                )
                + "只返回严格 JSON："
                '{"results":[{"subquestion_id":"sq_1",'
                '"sufficiency":"SUFFICIENT|INSUFFICIENT","labels":["E1"],'
                '"missing_facts":[],"supplemental_query":null}]}。'
                "不得返回整体 coverage、解释或额外字段。",
            ),
            ChatMessage("user", "\n\n".join(evidence_sections)),
        ]
        correction = ChatMessage(
            "user",
            "上一响应不符合 VERIFY JSON contract。请逐题返回且只返回 results；"
            "检查 subquestion_id、sufficiency、labels、missing_facts 和单个 "
            "supplemental_query。",
        )
        while True:
            try:
                result = await self._chat.complete_json(
                    messages,
                    timeout_seconds=self._chat_timeout(context),
                    reasoning_budget_tokens=(
                        self._policy.limits.max_verify_thinking_tokens
                    ),
                )
                context.record_usage("VERIFY", result.usage)
                response = VerificationResponse.model_validate(result.payload)
                return self._validated_verification(plan, evidence, response)
            except (ValidationError, ValueError, ChatError) as exc:
                retryable = isinstance(exc, (ValidationError, ValueError)) or exc.retryable
                if retryable and context.consume_retry():
                    messages.append(correction)
                    continue
                raise RunExecutionError("MODEL_FAILED") from exc

    @staticmethod
    def _validated_verification(
        plan: RetrievalPlan,
        evidence: RetrievalEvidence,
        response: VerificationResponse,
    ) -> list[SubquestionVerification]:
        expected_ids = [item.id for item in plan.subquestions]
        actual_ids = [item.subquestion_id.strip() for item in response.results]
        if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_ids):
            raise ValueError("VERIFY results do not match PLAN subquestions")
        result_by_id = {
            item.subquestion_id.strip(): item for item in response.results
        }
        normalized: list[SubquestionVerification] = []
        for subquestion_id in expected_ids:
            item = result_by_id[subquestion_id]
            labels = list(dict.fromkeys(label.strip() for label in item.labels if label.strip()))
            allowed_labels = set(evidence.labels_by_subquestion.get(subquestion_id, ()))
            if not set(labels) <= allowed_labels:
                raise ValueError("VERIFY referenced evidence outside its subquestion")
            missing_facts = list(
                dict.fromkeys(fact.strip() for fact in item.missing_facts if fact.strip())
            )
            supplemental_query = (
                item.supplemental_query.strip()
                if item.supplemental_query and item.supplemental_query.strip()
                else None
            )
            if item.sufficiency == "SUFFICIENT":
                if not labels or missing_facts or supplemental_query is not None:
                    raise ValueError("SUFFICIENT verification is inconsistent")
            elif not missing_facts:
                raise ValueError("INSUFFICIENT verification requires missing facts")
            normalized.append(
                SubquestionVerification(
                    subquestion_id=subquestion_id,
                    sufficiency=item.sufficiency,
                    labels=labels,
                    missing_facts=missing_facts,
                    supplemental_query=supplemental_query,
                )
            )
        return normalized

    @staticmethod
    def _gap_queries(
        verification: list[SubquestionVerification],
    ) -> list[tuple[str, str]]:
        return [
            (item.subquestion_id, item.supplemental_query)
            for item in verification
            if item.sufficiency == "INSUFFICIENT"
            and item.supplemental_query is not None
        ]

    @classmethod
    def _subquestions_with_new_evidence(
        cls,
        gap_queries: list[tuple[str, str]],
        before: RetrievalEvidence,
        after: RetrievalEvidence,
    ) -> set[str]:
        return {
            subquestion_id
            for subquestion_id, _query in gap_queries
            if cls._evidence_identities(after, subquestion_id)
            - cls._evidence_identities(before, subquestion_id)
        }

    @staticmethod
    def _evidence_identities(
        evidence: RetrievalEvidence, subquestion_id: str
    ) -> set[tuple[str, str]]:
        return {
            (item.source_id, item.chunk_id)
            for label in evidence.labels_by_subquestion.get(subquestion_id, ())
            if (item := evidence.by_label.get(label)) is not None
        }

    @staticmethod
    def _merge_verification(
        plan: RetrievalPlan,
        first_round: list[SubquestionVerification],
        gap_updates: list[SubquestionVerification],
    ) -> list[SubquestionVerification]:
        by_id = {item.subquestion_id: item for item in first_round}
        by_id.update({item.subquestion_id: item for item in gap_updates})
        return [by_id[item.id] for item in plan.subquestions]

    @staticmethod
    def _coverage(verification: list[SubquestionVerification]) -> str:
        sufficient = sum(item.sufficiency == "SUFFICIENT" for item in verification)
        if sufficient == len(verification):
            return "FULL"
        if sufficient == 0:
            return "NONE"
        return "PARTIAL"

    @staticmethod
    def _verified_evidence(
        plan: RetrievalPlan,
        verification: list[SubquestionVerification],
        evidence: RetrievalEvidence,
    ) -> RetrievalEvidence:
        result_by_id = {item.subquestion_id: item for item in verification}
        labels_by_subquestion: dict[str, tuple[str, ...]] = {}
        by_label: dict[str, Evidence] = {}
        for subquestion in plan.subquestions:
            result = result_by_id[subquestion.id]
            if result.sufficiency != "SUFFICIENT":
                continue
            labels_by_subquestion[subquestion.id] = tuple(result.labels)
            for label in result.labels:
                by_label[label] = evidence.by_label[label]
        return RetrievalEvidence(
            by_label=by_label,
            labels_by_subquestion=labels_by_subquestion,
        )

    @staticmethod
    def _answered_subquestions(
        plan: RetrievalPlan, verification: list[SubquestionVerification]
    ) -> list[PlannedSubquestion]:
        sufficient_ids = {
            item.subquestion_id
            for item in verification
            if item.sufficiency == "SUFFICIENT"
        }
        return [item for item in plan.subquestions if item.id in sufficient_ids]

    @staticmethod
    def _partial_notice(
        plan: RetrievalPlan, verification: list[SubquestionVerification]
    ) -> str:
        questions = {item.id: item.question for item in plan.subquestions}
        gaps = [
            f"- {questions[item.subquestion_id]}：{'；'.join(item.missing_facts)}"
            for item in verification
            if item.sufficiency == "INSUFFICIENT"
        ]
        return "\n\n以下问题因证据不足未回答：\n" + "\n".join(gaps)

    @staticmethod
    def _refusal(
        plan: RetrievalPlan, verification: list[SubquestionVerification]
    ) -> str:
        questions = {item.id: item.question for item in plan.subquestions}
        gaps = [
            f"{questions[item.subquestion_id]}：{'；'.join(item.missing_facts)}"
            for item in verification
        ]
        return "当前证据不足，无法可靠回答。缺少：" + "；".join(gaps) + "。"

    async def _generate_grounded(
        self,
        prepared: PreparedContext,
        content: str,
        decision: RouteDecision,
        subquestions: list[PlannedSubquestion],
        evidence: RetrievalEvidence,
        context: RunExecutionContext,
    ) -> AsyncIterator[str]:
        evidence_sections: list[str] = []
        for subquestion in subquestions:
            labels = evidence.labels_by_subquestion[subquestion.id]
            rendered = "\n".join(
                f"[{label}] {evidence.by_label[label].filename} 第 "
                f"{','.join(map(str, evidence.by_label[label].pages))} 页："
                f"{evidence.by_label[label].text}"
                for label in labels
            )
            evidence_sections.append(
                f"子问题 {subquestion.id}：{subquestion.question}\n"
                f"该题允许证据：\n{rendered}"
            )
        messages = [
            ChatMessage(
                "system",
                "只回答列出的已核验证据充分子问题，只使用给定 PDF 证据，"
                "不补充通用知识，不回答其他子问题。每个子问题正文前必须先输出且只输出"
                f"一个内部证据关联头：{GROUNDING_CONTROL_PREFIX}"
                "<subquestion_id>|<evidence_label,evidence_label>"
                f"{GROUNDING_CONTROL_SUFFIX}。关联头必须使用该题允许证据中的至少一个"
                "label；不得省略、重复、跨题关联或在正文中再次输出关联头。",
            ),
            *prepared.chat_messages(content),
            ChatMessage(
                "system",
                f"独立问题：{decision.standalone_question}\n"
                + "\n\n".join(evidence_sections),
            ),
        ]
        parser = GroundedAnswerParser(evidence.labels_by_subquestion)
        async for event in self._chat.stream_text(
            messages,
            timeout_seconds=self._chat_timeout(context),
            reasoning_budget_tokens=self._policy.limits.max_answer_thinking_tokens,
        ):
            if isinstance(event, ChatStreamUsage):
                context.record_usage("ANSWER", event.usage)
            else:
                visible = parser.feed(event.delta)
                if visible:
                    yield visible
        associations = parser.finish()
        context.evidence = self._associated_evidence(evidence, associations)
        context.evidence_token_count = sum(
            self._retrieval.token_count(item) for item in context.evidence
        )
        context.citations = self._citations(context.evidence)

    @staticmethod
    def _associated_evidence(
        evidence: RetrievalEvidence,
        associations: tuple[tuple[str, tuple[str, ...]], ...],
    ) -> list[Evidence]:
        selected: list[Evidence] = []
        seen: set[tuple[str, str]] = set()
        for _subquestion_id, labels in associations:
            for label in labels:
                item = evidence.by_label[label]
                key = (item.source_id, item.chunk_id)
                if key not in seen:
                    seen.add(key)
                    selected.append(item)
        return selected

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

    @classmethod
    def _deterministic_route(
        cls, source_scope: dict[str, object], content: str
    ) -> AnswerRoute:
        knowledge_mode = source_scope.get("knowledge_mode")
        if knowledge_mode not in {"DISABLED", "ALL", "SELECTED"}:
            raise RuntimeError("Answering run source scope is invalid")
        if knowledge_mode in {"ALL", "SELECTED"}:
            return AnswerRoute.RETRIEVAL
        if cls._scope_ids(source_scope, "attachment_ids"):
            return AnswerRoute.RETRIEVAL
        normalized = content.casefold()
        if "pdf" in normalized or any(
            marker in content for marker in ("附件", "资料", "文档", "知识库")
        ):
            return AnswerRoute.RETRIEVAL
        return AnswerRoute.GENERAL

    async def _understand(
        self,
        prepared: PreparedContext,
        content: str,
        context: RunExecutionContext,
    ) -> UnderstandingResult:
        messages = [
            ChatMessage(
                "system",
                "结合受限会话上下文，把当前问题改写为可独立理解的问题。"
                '只返回严格 JSON 对象：{"standalone_question":"..."}。'
                "不得返回 route、reason_code、解释或额外字段。",
            ),
            *prepared.chat_messages(content),
        ]
        correction = ChatMessage(
            "user",
            "上一响应不符合 UNDERSTAND JSON contract。请只返回非空 "
            "standalone_question。",
        )
        while True:
            try:
                result = await self._chat.complete_json(
                    messages,
                    timeout_seconds=self._chat_timeout(context),
                )
                context.record_usage("UNDERSTAND", result.usage)
                understanding = UnderstandingResult.model_validate(result.payload)
                standalone_question = understanding.standalone_question.strip()
                if not standalone_question:
                    raise ValueError("UNDERSTAND contains an empty standalone question")
                return UnderstandingResult(standalone_question=standalone_question)
            except (ValidationError, ValueError):
                if context.consume_retry():
                    messages.append(correction)
                    continue
                raise
            except ChatError as exc:
                if exc.retryable and context.consume_retry():
                    messages.append(correction)
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
        async with self._sessions() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
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
            run.route = context.route
            run.error_code = None
            run.finished_at = now
            run.trace_version = self._policy.trace_version
            run.policy_version = self._policy.policy_version
            run.trace_json = await self._terminal_trace(session, run, context, now, "SUCCEEDED")
            assistant_message.content = context.answer
            assistant_message.status = MessageStatus.COMPLETE
            assistant_message.citations_json = context.citations
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
        context: RunExecutionContext,
        error_code: str,
    ) -> None:
        now = self._utc_now()
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
            run.route = context.route
            run.error_code = error_code
            run.finished_at = now
            run.trace_version = self._policy.trace_version
            run.policy_version = self._policy.policy_version
            run.trace_json = await self._terminal_trace(session, run, context, now, error_code)
            assistant_message.content = context.answer
            assistant_message.status = MessageStatus.PARTIAL
            assistant_message.citations_json = []

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

    async def recover_interrupted_runs(self) -> None:
        now = self._utc_now()
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
        return RunTraceV1(
            versions=TraceVersions(
                trace_version=self._policy.trace_version,
                policy_version=self._policy.policy_version,
            ),
            route=None if context.route is None else context.route.value,
            coverage=context.coverage,
            usage=TraceUsage(
                calls=calls,
                request_count=len(calls),
                prompt_tokens=sum(call.prompt_tokens for call in calls),
                completion_tokens=sum(call.completion_tokens for call in calls),
                total_tokens=sum(call.total_tokens for call in calls),
            ),
            timing=TraceTiming(total_ms=total_ms, stages=stage_timings),
            retry=TraceRetry(
                limit=self._policy.limits.max_retries,
                used=context.retries_used,
            ),
            retrieval=TraceRetrieval(
                retrieval_round_count=context.retrieval_round_count,
                actual_query_count=context.actual_query_count,
                planned_subquestion_count=context.planned_subquestion_count,
                sufficient_subquestion_count=context.sufficient_subquestion_count,
                missing_fact_count=context.missing_fact_count,
                final_evidence_source_types=sorted(
                    {item.source_type for item in context.evidence}
                ),
                final_evidence_chunk_count=len(context.evidence),
                final_evidence_token_count=context.evidence_token_count,
            ),
            termination=(None if termination is None else TraceTermination(reason=termination)),
        )

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
