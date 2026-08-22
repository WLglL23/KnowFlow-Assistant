from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
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
    RetrievalError,
    RetrievalSource,
)
from knowflow.knowledge.answering.trace import (
    RunTraceV1,
    TraceEvidenceSource,
    TraceRetrieval,
    TraceRetrievalLimits,
    TraceRetry,
    TraceStageTiming,
    TraceSubquestion,
    TraceTermination,
    TraceTiming,
    TraceUsage,
    TraceUsageCall,
    TraceVersions,
)
from knowflow.knowledge.conversations.models import Conversation
from knowflow.knowledge.conversations.service import allowed_source_scope
from knowflow.knowledge.documents.models import ConversationAttachment, KnowledgeSubmission


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: AnswerRoute
    standalone_question: str
    reason_code: str


class RetrievalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[str]


@dataclass(frozen=True, slots=True)
class Subquestion:
    id: str
    query: str


class SubquestionVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subquestion_id: str
    sufficient: bool
    missing_facts: list[str]
    supplemental_query: str | None = None


class VerificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[SubquestionVerification]


class LegacyVerificationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage: Literal["FULL", "NONE"]
    missing_facts: list[str]


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
    missing_facts: list[str] = field(default_factory=lambda: [])
    actual_queries: int = 0
    retrieval_rounds: int = 0
    verifications: list[SubquestionVerification] = field(default_factory=lambda: [])

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
        sources = await self._retrieval_sources(run_id)
        try:
            decision = await self._route(
                prepared,
                content,
                has_allowed_sources=bool(sources),
                context=context,
            )
        except (ChatError, ValidationError) as exc:
            raise RunExecutionError("MODEL_FAILED") from exc
        context.route = decision.route
        await self._complete_understand_stage(context.current_stage_id, run_id, decision.route)
        context.current_stage_id = None
        context.current_stage = None
        yield StageEvent("stage.completed", request_id, run_id, StageName.UNDERSTAND.value)

        if decision.route is AnswerRoute.RETRIEVAL:
            if sources:
                await self._enter_stage(run_id, StageName.PLAN, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.PLAN.value)
                plan = await self._plan(prepared, decision, context)
                subquestions = [
                    Subquestion(id=f"sq_{index}", query=query)
                    for index, query in enumerate(plan.queries, 1)
                ]
                await self._complete_stage(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.PLAN.value)

                await self._enter_stage(run_id, StageName.RETRIEVE_1, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.RETRIEVE_1.value)
                context.evidence = await self._retrieve(subquestions, sources, context)
                context.evidence, context.evidence_token_count = self._retrieval.select(
                    context.evidence,
                    max_chunks=self._policy.limits.max_evidence_chunks,
                    max_tokens=self._policy.limits.max_evidence_tokens,
                    required_subquestion_ids={item.id for item in subquestions},
                )
                await self._complete_stage(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.RETRIEVE_1.value)

                await self._enter_stage(run_id, StageName.VERIFY, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.VERIFY.value)
                verifications = await self._verify(subquestions, context.evidence, context)
                context.verifications = verifications
                context.coverage = self._aggregate_coverage(verifications)
                await self._complete_stage(context.current_stage_id)
                context.current_stage_id = None
                context.current_stage = None
                yield StageEvent("stage.completed", request_id, run_id, StageName.VERIFY.value)

                supplemental = [
                    Subquestion(item.subquestion_id, item.supplemental_query.strip())
                    for item in verifications
                    if not item.sufficient
                    and item.supplemental_query is not None
                    and item.supplemental_query.strip()
                ]
                if supplemental:
                    await self._enter_stage(run_id, StageName.RETRIEVE_2, context)
                    yield StageEvent(
                        "stage.started", request_id, run_id, StageName.RETRIEVE_2.value
                    )
                    additional = await self._retrieve(
                        supplemental,
                        sources,
                        context,
                    )
                    context.evidence = self._deduplicate_evidence(
                        [*additional, *context.evidence]
                    )
                    context.evidence, context.evidence_token_count = self._retrieval.select(
                        context.evidence,
                        max_chunks=self._policy.limits.max_evidence_chunks,
                        max_tokens=self._policy.limits.max_evidence_tokens,
                        required_subquestion_ids={item.id for item in subquestions},
                    )
                    await self._complete_stage(context.current_stage_id)
                    context.current_stage_id = None
                    context.current_stage = None
                    yield StageEvent(
                        "stage.completed", request_id, run_id, StageName.RETRIEVE_2.value
                    )

                    await self._enter_stage(run_id, StageName.VERIFY, context)
                    yield StageEvent("stage.started", request_id, run_id, StageName.VERIFY.value)
                    gap_results = await self._verify(
                        supplemental, context.evidence, context
                    )
                    gap_by_id = {item.subquestion_id: item for item in gap_results}
                    verifications = [
                        gap_by_id.get(item.subquestion_id, item) for item in verifications
                    ]
                    context.verifications = verifications
                    context.coverage = self._aggregate_coverage(verifications)
                    await self._complete_stage(context.current_stage_id)
                    context.current_stage_id = None
                    context.current_stage = None
                    yield StageEvent("stage.completed", request_id, run_id, StageName.VERIFY.value)

                context.missing_facts = list(
                    dict.fromkeys(
                        fact
                        for item in verifications
                        if not item.sufficient
                        for fact in item.missing_facts
                    )
                )
                sufficient_ids = {
                    item.subquestion_id for item in verifications if item.sufficient
                }
                final_candidates = [
                    item
                    for item in context.evidence
                    if item.subquestion_id in sufficient_ids
                ]
                context.evidence, context.evidence_token_count = self._retrieval.select(
                    final_candidates,
                    max_chunks=self._policy.limits.max_evidence_chunks,
                    max_tokens=self._policy.limits.max_evidence_tokens,
                    required_subquestion_ids=sufficient_ids,
                )
                if sufficient_ids - {item.subquestion_id for item in context.evidence}:
                    raise RunExecutionError("INTERNAL_ERROR")
                await self._enter_stage(run_id, StageName.GENERATE, context)
                yield StageEvent("stage.started", request_id, run_id, StageName.GENERATE.value)
                try:
                    if context.coverage in {"FULL", "PARTIAL"}:
                        context.citations = self._citations(context.evidence)
                        self._validate_citations(
                            context.evidence, context.citations, sufficient_ids
                        )
                        async for delta in self._generate_grounded(
                            prepared, content, decision, context.evidence, context
                        ):
                            visible, exceeded = self._append_visible_delta(context, delta)
                            if visible:
                                yield AnswerDeltaEvent("answer.delta", request_id, run_id, visible)
                            if exceeded:
                                raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                        if context.coverage == "PARTIAL":
                            suffix = self._partial_suffix(verifications)
                            visible, exceeded = self._append_visible_delta(context, suffix)
                            if visible:
                                yield AnswerDeltaEvent("answer.delta", request_id, run_id, visible)
                            if exceeded:
                                raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                    else:
                        refusal = self._none_refusal(verifications)
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

    async def _retrieval_sources(self, run_id: str) -> dict[str, RetrievalSource]:
        async with self._sessions() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise RuntimeError("Answering run is missing")
            raw_attachment_ids = run.source_scope_json.get("attachment_ids")
            raw_source_ids = run.source_scope_json.get("knowledge_source_ids")
            if not isinstance(raw_attachment_ids, list) or not isinstance(raw_source_ids, list):
                raise RuntimeError("Answering run source scope is invalid")
            attachment_values = cast(list[object], raw_attachment_ids)
            source_values = cast(list[object], raw_source_ids)
            if not all(isinstance(item, str) for item in [*attachment_values, *source_values]):
                raise RuntimeError("Answering run source scope is invalid")
            attachment_ids = cast(list[str], attachment_values)
            source_ids = cast(list[str], source_values)
            attachments = list(
                (
                    await session.scalars(
                        select(ConversationAttachment).where(
                            ConversationAttachment.id.in_(attachment_ids)
                        )
                    )
                ).all()
            )
            sources = list(
                (
                    await session.scalars(
                        select(KnowledgeSubmission).where(
                            KnowledgeSubmission.id.in_(source_ids)
                        )
                    )
                ).all()
            )
        sources_by_id = {
            item.id: RetrievalSource(
                source_type="ATTACHMENT",
                filename=item.filename,
                page_count=item.page_count,
            )
            for item in attachments
            if item.id in attachment_ids
        }
        sources_by_id.update(
            {
                item.id: RetrievalSource(
                    source_type="KNOWLEDGE",
                    filename=item.filename,
                    page_count=item.page_count,
                )
                for item in sources
                if item.id in source_ids
            }
        )
        return sources_by_id

    async def _plan(
        self, prepared: PreparedContext, decision: RouteDecision, context: RunExecutionContext
    ) -> RetrievalPlan:
        messages = [
            ChatMessage(
                "system", "为独立 PDF 问题生成一个检索查询。只返回 JSON：{\"queries\":[\"...\"]}。"
            ),
            *prepared.chat_messages(decision.standalone_question),
        ]
        while True:
            try:
                result = await self._chat.complete_json(
                    messages, timeout_seconds=self._chat_timeout(context)
                )
                context.record_usage("PLAN", result.usage)
                plan = RetrievalPlan.model_validate(result.payload)
                queries = [query.strip() for query in plan.queries if query.strip()]
                if not queries:
                    raise ValidationError.from_exception_data("RetrievalPlan", [])
                if len(queries) > self._policy.limits.max_subquestions:
                    raise RunExecutionError("RUN_LIMIT_EXCEEDED")
                return RetrievalPlan(queries=list(dict.fromkeys(queries)))
            except (ValidationError, ChatError) as exc:
                retryable = isinstance(exc, ValidationError) or exc.retryable
                if retryable and context.consume_retry():
                    continue
                raise RunExecutionError("MODEL_FAILED") from exc

    async def _retrieve(
        self,
        subquestions: list[Subquestion],
        sources: dict[str, RetrievalSource],
        context: RunExecutionContext,
    ) -> list[Evidence]:
        queries = [item.query for item in subquestions]
        if context.retrieval_rounds >= self._policy.limits.max_retrieval_rounds:
            raise RunExecutionError("RUN_LIMIT_EXCEEDED")
        if context.actual_queries + len(queries) > self._policy.limits.max_actual_queries:
            raise RunExecutionError("RUN_LIMIT_EXCEEDED")
        context.retrieval_rounds += 1
        context.actual_queries += len(queries)
        while True:
            try:
                vectors = await self._retrieval.embed_many(
                    queries, timeout_seconds=self._chat_timeout(context)
                )
                break
            except RetrievalError as exc:
                if not (exc.retryable and context.consume_retry()):
                    raise RunExecutionError("RETRIEVAL_FAILED") from exc
        evidence: list[Evidence] = []
        for subquestion, vector in zip(subquestions, vectors, strict=True):
            try:
                hits = await self._retrieval.hybrid(
                    subquestion.query,
                    vector,
                    sources,
                    timeout_seconds=self._chat_timeout(context),
                )
                associated = [
                    replace(item, subquestion_id=subquestion.id) for item in hits
                ]
                evidence.extend(
                    await self._rerank(
                        subquestion.query,
                        associated,
                        context,
                    )
                )
            except RetrievalError as exc:
                raise RunExecutionError("RETRIEVAL_FAILED") from exc
        return self._deduplicate_evidence(evidence)

    async def _rerank(
        self,
        query: str,
        evidence: list[Evidence],
        context: RunExecutionContext,
    ) -> list[Evidence]:
        while True:
            try:
                return await self._retrieval.rerank(
                    query, evidence, timeout_seconds=self._chat_timeout(context)
                )
            except RetrievalError as exc:
                if not (exc.retryable and context.consume_retry()):
                    raise RunExecutionError("RETRIEVAL_FAILED") from exc

    @staticmethod
    def _deduplicate_evidence(evidence: list[Evidence]) -> list[Evidence]:
        by_chunk: dict[tuple[str, str], Evidence] = {}
        for item in evidence:
            by_chunk.setdefault((item.chunk_id, item.subquestion_id), item)
        return list(by_chunk.values())

    @staticmethod
    def _partial_suffix(results: list[SubquestionVerification]) -> str:
        gaps = [
            f"{item.subquestion_id}（{'、'.join(item.missing_facts) or '证据不足'}）"
            for item in results
            if not item.sufficient
        ]
        return f"\n\n仍无法回答：{'；'.join(gaps)}。当前证据不足。"

    @staticmethod
    def _none_refusal(results: list[SubquestionVerification]) -> str:
        gaps = [
            f"{item.subquestion_id}（{'、'.join(item.missing_facts) or '证据不足'}）"
            for item in results
        ]
        return (
            "当前证据不足，无法可靠回答："
            + "；".join(gaps)
            + "。请补充包含相关事实的 PDF。"
        )

    async def _verify(
        self,
        subquestions: list[Subquestion],
        evidence: list[Evidence],
        context: RunExecutionContext,
    ) -> list[SubquestionVerification]:
        evidence_text = "\n\n".join(
            f"子问题：{item.subquestion_id} 来源：{item.filename} "
            f"页码：{','.join(map(str, item.pages))}\n{item.text}"
            for item in evidence
        )
        messages = [
            ChatMessage(
                "system",
                "分别核验证据是否足以回答每个子问题。每题最多提供一个补充查询。"
                "只返回逐题 JSON 结果。",
            ),
            ChatMessage(
                "user",
                "子问题："
                + "；".join(f"{item.id}:{item.query}" for item in subquestions)
                + f"\n证据：\n{evidence_text}",
            ),
        ]
        while True:
            try:
                result = await self._chat.complete_json(
                    messages, timeout_seconds=self._chat_timeout(context)
                )
                context.record_usage("VERIFY", result.usage)
                return self._verification_results(result.payload, subquestions)
            except (ValidationError, ChatError) as exc:
                retryable = isinstance(exc, ValidationError) or exc.retryable
                if retryable and context.consume_retry():
                    continue
                raise RunExecutionError("MODEL_FAILED") from exc

    @staticmethod
    def _verification_results(
        payload: dict[str, object], subquestions: list[Subquestion]
    ) -> list[SubquestionVerification]:
        try:
            response = VerificationResponse.model_validate(payload)
            results = response.results
        except ValidationError:
            if len(subquestions) != 1:
                raise
            legacy = LegacyVerificationDecision.model_validate(payload)
            results = [
                SubquestionVerification(
                    subquestion_id=subquestions[0].id,
                    sufficient=legacy.coverage == "FULL",
                    missing_facts=legacy.missing_facts,
                )
            ]
        expected = {item.id for item in subquestions}
        actual = [item.subquestion_id for item in results]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValidationError.from_exception_data("VerificationResponse", [])
        return [
            item.model_copy(
                update={
                    "missing_facts": list(dict.fromkeys(item.missing_facts)),
                    "supplemental_query": (
                        item.supplemental_query.strip()
                        if item.supplemental_query and item.supplemental_query.strip()
                        else None
                    ),
                }
            )
            for item in results
        ]

    @staticmethod
    def _aggregate_coverage(results: list[SubquestionVerification]) -> str:
        sufficient = sum(item.sufficient for item in results)
        if sufficient == len(results):
            return "FULL"
        if sufficient:
            return "PARTIAL"
        return "NONE"

    async def _generate_grounded(
        self,
        prepared: PreparedContext,
        content: str,
        decision: RouteDecision,
        evidence: list[Evidence],
        context: RunExecutionContext,
    ) -> AsyncIterator[str]:
        evidence_text = "\n\n".join(
            f"{item.filename} 第 {','.join(map(str, item.pages))} 页：{item.text}"
            for item in evidence
        )
        messages = [
            ChatMessage("system", "只根据给定 PDF 证据回答，不补充通用知识，不输出引用标记。"),
            *prepared.chat_messages(content),
            ChatMessage(
                "system",
                f"独立问题：{decision.standalone_question}\n证据：\n{evidence_text}",
            ),
        ]
        async for event in self._chat.stream_text(
            messages, timeout_seconds=self._chat_timeout(context)
        ):
            if isinstance(event, ChatStreamUsage):
                context.record_usage("ANSWER", event.usage)
            else:
                yield event.delta

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

    @staticmethod
    def _validate_citations(
        evidence: list[Evidence],
        citations: list[dict[str, object]],
        sufficient_ids: set[str],
    ) -> None:
        cited_source_ids = {
            item.get("source_id") for item in citations if isinstance(item.get("source_id"), str)
        }
        supported_ids = {
            item.subquestion_id
            for item in evidence
            if item.source_id in cited_source_ids and item.pages
        }
        if supported_ids != sufficient_ids:
            raise RunExecutionError("INTERNAL_ERROR")

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

    async def _route(
        self,
        prepared: PreparedContext,
        content: str,
        *,
        has_allowed_sources: bool,
        context: RunExecutionContext,
    ) -> RouteDecision:
        source_hint = "有" if has_allowed_sources else "无"
        messages = [
            ChatMessage(
                "system",
                "判断问题应走 GENERAL 还是 RETRIEVAL。只返回 JSON："
                '{"route":"GENERAL|RETRIEVAL","standalone_question":"...",'
                f'"reason_code":"..."}}。当前 Run 已冻结允许 PDF 来源：{source_hint}。'
                "有允许来源时，定义、事实、比较、流程和资料内容等自然知识问题优先选择 "
                "RETRIEVAL；仅闲聊、创作、计算或明确无需资料时选择 GENERAL。"
                "没有允许来源但明确要求 PDF 或资料证据时仍选择 RETRIEVAL。",
            ),
            *prepared.chat_messages(content),
        ]
        while True:
            try:
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
        sources: list[TraceEvidenceSource] = []
        for item in context.evidence:
            current = next(
                (source for source in sources if source.source_id == item.source_id),
                None,
            )
            if current is None:
                sources.append(
                    TraceEvidenceSource(
                        source_type=item.source_type,
                        source_id=item.source_id,
                        pages=list(item.pages[:5]),
                    )
                )
            else:
                current.pages = list(dict.fromkeys([*current.pages, *item.pages]))[:5]
        subquestions: list[TraceSubquestion] = []
        for verification in context.verifications:
            item_sources: list[TraceEvidenceSource] = []
            for item in context.evidence:
                if item.subquestion_id != verification.subquestion_id:
                    continue
                current = next(
                    (
                        source
                        for source in item_sources
                        if source.source_id == item.source_id
                    ),
                    None,
                )
                if current is None:
                    item_sources.append(
                        TraceEvidenceSource(
                            source_type=item.source_type,
                            source_id=item.source_id,
                            pages=list(item.pages[:5]),
                        )
                    )
                else:
                    current.pages = list(
                        dict.fromkeys([*current.pages, *item.pages])
                    )[:5]
            subquestions.append(
                TraceSubquestion(
                    subquestion_id=verification.subquestion_id,
                    sufficient=verification.sufficient,
                    missing_facts=verification.missing_facts,
                    sources=item_sources,
                )
            )
        limits = self._policy.limits
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
                final_evidence_chunk_count=len(context.evidence),
                final_evidence_token_count=context.evidence_token_count,
                actual_query_count=context.actual_queries,
                retrieval_round_count=context.retrieval_rounds,
                missing_facts=context.missing_facts,
                sources=sources,
                subquestions=subquestions,
                limits=TraceRetrievalLimits(
                    max_subquestions=limits.max_subquestions,
                    max_retrieval_rounds=limits.max_retrieval_rounds,
                    max_actual_queries=limits.max_actual_queries,
                    max_evidence_chunks=limits.max_evidence_chunks,
                    max_evidence_tokens=limits.max_evidence_tokens,
                ),
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
