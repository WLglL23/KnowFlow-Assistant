from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TraceVersions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_version: str
    policy_version: str


class TraceUsageCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class TraceUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: list[TraceUsageCall]
    request_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class TraceStageTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    duration_ms: int


class TraceTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_ms: int
    stages: list[TraceStageTiming]


class TraceRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int
    used: int


class TraceTermination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


class TraceRetrieval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_evidence_chunk_count: int
    final_evidence_token_count: int
    query_count: int = 0
    retrieval_round_count: int = 0
    initial_candidate_count: int = 0
    rerank_candidate_count: int = 0
    duplicate_chunk_count: int = 0
    source_type_count: int = 0


class TraceLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_limit_compliance: bool | None = None
    visible_character_limit_compliance: bool | None = None
    budget_limit_compliance: bool | None = None
    source_scope_compliance: bool | None = None


class TraceEvidenceLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id_hash: str
    chunk_id_hash: str
    document_title: str | None = None
    headings: list[str] = Field(default_factory=list)
    pages: list[int] = Field(default_factory=lambda: list[int]())


class TraceSubquestionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subquestion_id_hash: str
    retrieval_round_count: int
    initial_candidate_count: int
    rerank_candidate_count: int
    selected_evidence_count: int
    selected_evidence_locations: list[TraceEvidenceLocation] = Field(
        default_factory=lambda: list[TraceEvidenceLocation]()
    )
    used_evidence_locations: list[TraceEvidenceLocation] = Field(
        default_factory=lambda: list[TraceEvidenceLocation]()
    )
    selected_evidence_token_count: int
    final_evidence_count: int = 0
    final_evidence_token_count: int = 0
    input_reference_page_hit: bool | None
    initial_candidate_page_hashes: list[str] = Field(default_factory=lambda: list[str]())
    rerank_candidate_page_hashes: list[str] = Field(default_factory=lambda: list[str]())
    verify_input_page_hashes: list[str]
    final_evidence_page_hashes: list[str] = Field(default_factory=lambda: list[str]())
    verify_label_count: int
    verify_missing_fact_count: int
    has_supplemental_query: bool
    second_round_identity_changed: bool | None
    second_round_useful: bool | None
    answer_executed: bool
    citation_presence: bool | None
    citation_integrity: bool | None


class RunTraceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versions: TraceVersions
    route: str | None
    usage: TraceUsage
    timing: TraceTiming
    retry: TraceRetry
    retrieval: TraceRetrieval | None = None
    limits: TraceLimits | None = None
    subquestions: list[TraceSubquestionObservation] = Field(
        default_factory=lambda: list[TraceSubquestionObservation]()
    )
    termination: TraceTermination | None = None

    def as_json(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)
