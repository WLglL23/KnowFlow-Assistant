from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class StageEvent:
    type: str
    request_id: str
    run_id: str
    stage: str


@dataclass(frozen=True, slots=True)
class AnswerDeltaEvent:
    type: str
    request_id: str
    run_id: str
    delta: str


@dataclass(frozen=True, slots=True)
class RunCompletedEvent:
    type: str
    request_id: str
    run_id: str
    assistant_message_id: str


@dataclass(frozen=True, slots=True)
class RunFailedEvent:
    type: str
    request_id: str
    run_id: str
    assistant_message_id: str
    error: dict[str, str]


type AnsweringEvent = StageEvent | AnswerDeltaEvent | RunCompletedEvent | RunFailedEvent
type EventFactory = Callable[[], AsyncIterator[AnsweringEvent]]


@dataclass(slots=True)
class AcceptedTurn:
    conversation_id: str
    run_id: str
    user_message_id: str
    assistant_message_id: str
    request_id: str
    _event_factory: EventFactory = field(repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    async def events(self) -> AsyncIterator[AnsweringEvent]:
        if self._started:
            raise RuntimeError("AcceptedTurn.events() can only be consumed once")
        self._started = True
        async for event in self._event_factory():
            yield event
