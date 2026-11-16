from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowflow.knowledge.answering.execution import RunExecutionContext
from knowflow.knowledge.answering.models import (
    AgentRun,
    AgentRunStage,
    AnswerRoute,
    StageName,
    StageStatus,
)


class RunStages:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def enter(self, run_id: str, stage: StageName, context: RunExecutionContext) -> None:
        stage_id = f"stg_{uuid.uuid4().hex}"
        async with self._sessions() as session, session.begin():
            session.add(
                AgentRunStage(
                    id=stage_id,
                    run_id=run_id,
                    stage=stage,
                    status=StageStatus.RUNNING,
                    started_at=datetime.now(UTC).replace(tzinfo=None),
                    finished_at=None,
                    summary_json={},
                )
            )
        context.current_stage_id = stage_id
        context.current_stage = stage

    async def complete_understand(
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
            stage.finished_at = datetime.now(UTC).replace(tzinfo=None)
            run.route = route

    async def complete(self, stage_id: str | None) -> None:
        if stage_id is None:
            raise RuntimeError("Answering stage is missing")
        async with self._sessions() as session, session.begin():
            stage = await session.get(AgentRunStage, stage_id)
            if stage is None:
                raise RuntimeError("Answering stage is missing")
            stage.status = StageStatus.SUCCEEDED
            stage.finished_at = datetime.now(UTC).replace(tzinfo=None)

    async def fail_current(self, context: RunExecutionContext) -> None:
        if context.current_stage_id is None:
            return
        async with self._sessions() as session, session.begin():
            stage = await session.get(AgentRunStage, context.current_stage_id)
            if stage is not None and stage.status == StageStatus.RUNNING:
                stage.status = StageStatus.FAILED
                stage.finished_at = datetime.now(UTC).replace(tzinfo=None)
