from __future__ import annotations

from pydantic import BaseModel, ConfigDict


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


class RunTraceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    versions: TraceVersions
    route: str | None
    coverage: str | None = None
    usage: TraceUsage
    timing: TraceTiming
    retry: TraceRetry
    termination: TraceTermination | None = None

    def as_json(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)
