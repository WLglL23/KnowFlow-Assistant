from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from knowflow.knowledge.answering.chat import ChatUsage
from knowflow.knowledge.answering.models import AnswerRoute, StageName
from knowflow.knowledge.answering.outcomes import (
    AnsweringOutcome,
    LimitCompliance,
    SubquestionOutcome,
)
from knowflow.knowledge.answering.policy import RunPolicy
from knowflow.knowledge.answering.retrieval import Evidence, EvidenceTokenizerV1
from knowflow.knowledge.answering.trace import TraceUsageCall


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

    def timeout_seconds(self) -> float:
        remaining = self.remaining_seconds(datetime.now(UTC).replace(tzinfo=None))
        if remaining <= 0:
            raise RunExecutionError("RUN_LIMIT_EXCEEDED")
        return remaining
