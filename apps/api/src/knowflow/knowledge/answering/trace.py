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


class TraceEvidenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: str
    source_id: str
    pages: list[int]


class TraceSubquestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subquestion_id: str
    sufficient: bool
    missing_facts: list[str]
    sources: list[TraceEvidenceSource]


class TraceRetrievalLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_subquestions: int
    max_retrieval_rounds: int
    max_actual_queries: int
    max_evidence_chunks: int
    max_evidence_tokens: int


class TraceRetrieval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_evidence_chunk_count: int
    final_evidence_token_count: int
    actual_query_count: int = 0
    retrieval_round_count: int = 0
    missing_facts: list[str] = Field(default_factory=lambda: list[str]())
    sources: list[TraceEvidenceSource] = Field(
        default_factory=lambda: list[TraceEvidenceSource]()
    )
    subquestions: list[TraceSubquestion] = Field(
        default_factory=lambda: list[TraceSubquestion]()
    )
    limits: TraceRetrievalLimits


class RunTraceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versions: TraceVersions
    route: str | None
    coverage: str | None = None
    usage: TraceUsage
    timing: TraceTiming
    retry: TraceRetry
    retrieval: TraceRetrieval | None = None
    termination: TraceTermination | None = None

    def as_json(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)
