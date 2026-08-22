from __future__ import annotations

import json
from asyncio import CancelledError, create_task, wait
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from knowflow.app.container import AppContainer
from knowflow.app.errors import request_id_for
from knowflow.identity.api import current_account
from knowflow.identity.schemas import AccountResponse
from knowflow.knowledge.answering.contracts import AnsweringEvent
from knowflow.knowledge.answering.schemas import RunRequest

router = APIRouter(prefix="/api/conversations", tags=["conversation-runs"])
CurrentAccount = Annotated[AccountResponse, Depends(current_account)]


def container_for(request: Request) -> AppContainer:
    return request.app.state.container


@router.post("/{conversation_id}/runs")
async def create_run(
    conversation_id: str,
    payload: RunRequest,
    request: Request,
    account: CurrentAccount,
) -> Response:
    accepted = await container_for(request).answering.accept_turn(
        actor_id=account.id,
        conversation_id=conversation_id,
        content=payload.content,
        client_request_id=payload.client_request_id,
        request_id=request_id_for(request),
    )

    async def stream() -> AsyncIterator[bytes]:
        async for event in accepted.events():
            yield _encode_event(event)

    return RunEventStreamResponse(stream())


class RunEventStreamResponse(Response):
    media_type = "text/event-stream"

    def __init__(self, body_iterator: AsyncIterator[bytes]) -> None:
        super().__init__(content=None, headers={"Cache-Control": "no-cache"})
        self.body_iterator = body_iterator
        self.raw_headers = [
            item for item in self.raw_headers if item[0].lower() != b"content-length"
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        stream_task = create_task(self._stream(send))
        disconnect_task = create_task(self._wait_for_disconnect(receive))
        done, _pending = await wait(
            {stream_task, disconnect_task},
            return_when="FIRST_COMPLETED",
        )
        if disconnect_task in done:
            stream_task.cancel()
            with suppress(CancelledError):
                await stream_task
            return
        disconnect_task.cancel()
        with suppress(CancelledError):
            await disconnect_task
        await stream_task

    async def _stream(self, send: Send) -> None:
        async for chunk in self.body_iterator:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    @staticmethod
    async def _wait_for_disconnect(receive: Receive) -> None:
        while True:
            message: Message = await receive()
            if message["type"] == "http.disconnect":
                return


def _encode_event(event: AnsweringEvent) -> bytes:
    raw = asdict(event)
    if event.type == "stage.started" or event.type == "stage.completed":
        event_data = {key: raw[key] for key in ("request_id", "run_id", "stage")}
    elif event.type == "answer.delta":
        event_data = {key: raw[key] for key in ("request_id", "run_id", "delta")}
    elif event.type == "run.completed":
        event_data = {
            key: raw[key]
            for key in ("request_id", "run_id", "assistant_message_id")
        }
    elif event.type == "run.failed":
        event_data = {
            key: raw[key]
            for key in ("request_id", "run_id", "assistant_message_id", "error")
        }
    else:
        raise ValueError(f"unsupported answering event: {event.type}")
    payload = json.dumps(event_data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.type}\ndata: {payload}\n\n".encode()
