from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowflow.knowledge.answering.execution import RunExecutionContext
from knowflow.knowledge.answering.models import AgentRun, AgentRunStage
from knowflow.knowledge.answering.observation_projection import SafeObservationProjector
from knowflow.knowledge.answering.policy import RunPolicy
from knowflow.knowledge.answering.trace import (
    RunTraceV1,
    TraceRetry,
    TraceStageTiming,
    TraceTiming,
    TraceUsage,
    TraceVersions,
)
from knowflow.knowledge.answering.trace_projection import TraceProjectionMetadata, TraceProjector


class RunDiagnostics:
    def __init__(self, policy: RunPolicy) -> None:
        self._policy = policy

    async def terminal_trace(
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
        trace = self.trace_model(
            context,
            total_ms=self._duration_ms(run.started_at, finished_at),
            stage_timings=stage_timings,
            termination=termination,
        )
        return trace.as_json()

    def trace_without_stages(
        self,
        context: RunExecutionContext,
        *,
        total_ms: int,
        termination: str | None,
    ) -> dict[str, object]:
        return self.trace_model(
            context,
            total_ms=total_ms,
            stage_timings=[],
            termination=termination,
        ).as_json()

    def trace_model(
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

    def observation(
        self, context: RunExecutionContext, failure_code: str | None
    ) -> dict[str, object]:
        return SafeObservationProjector().project(context.outcome(), failure_code=failure_code)

    @staticmethod
    def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
        return max(0, round((finished_at - started_at).total_seconds() * 1000))
