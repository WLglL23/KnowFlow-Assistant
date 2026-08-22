from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from knowflow.knowledge.answering.retrieval import Evidence

SubquestionStatus = Literal["ANSWERED", "INSUFFICIENT", "FAILED"]
RunOutcomeStatus = Literal["SUCCEEDED", "FAILED", "PARTIAL"]
AnswerRouteValue = Literal["GENERAL", "RETRIEVAL"]


@dataclass(frozen=True, slots=True)
class LimitCompliance:
    run: bool | None = None
    visible_characters: bool | None = None
    budget: bool | None = None
    source_scope: bool | None = None


@dataclass(slots=True)
class SubquestionOutcome:
    """The only business result for one planned subquestion."""

    subquestion_id_hash: str
    query: str
    status: SubquestionStatus
    answer: str | None
    evidence: list[Evidence]
    citations: list[dict[str, object]]
    failure_code: str | None
    answer_executed: bool
    used_evidence: list[Evidence] = field(default_factory=lambda: list[Evidence]())
    missing_facts: list[str] = field(default_factory=lambda: list[str]())
    retrieval_round_count: int = 0
    initial_candidate_count: int = 0
    rerank_candidate_count: int = 0
    selected_evidence_token_count: int = 0
    initial_candidate_page_hashes: list[str] = field(
        default_factory=lambda: list[str]()
    )
    verify_input_page_hashes: set[str] = field(default_factory=lambda: set[str]())
    rerank_candidate_page_hashes: list[str] = field(default_factory=lambda: list[str]())
    label_count: int = 0
    citation_presence: bool | None = None
    citation_integrity: bool | None = None
    has_supplemental_query: bool = False
    second_round_identity_changed: bool | None = None
    second_round_useful: bool | None = None


@dataclass(slots=True)
class AnsweringOutcome:
    """Run result projected by persistence, events, observation and trace."""

    run_status: RunOutcomeStatus
    route: AnswerRouteValue
    subquestions: list[SubquestionOutcome]
    citations: list[dict[str, object]]
    failure_code: str | None
    limit_compliance: LimitCompliance
    answer: str = ""
    query_count: int = 0
    retrieval_round_count: int = 0
    duplicate_chunk_count: int = 0
    source_type_count: int = 0
    selected_evidence_token_count: int = 0
    model_call_count: int = 0
    latency_ms_total: int | None = None
