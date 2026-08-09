from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from pydantic import SecretStr


class ChatConfigurationError(RuntimeError):
    pass


class ChatError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


class ChatClient(Protocol):
    def ensure_configured(self) -> None: ...

    async def complete_text(self, messages: Sequence[ChatMessage]) -> str: ...

    async def complete_json(self, messages: Sequence[ChatMessage]) -> dict[str, Any]: ...

    def stream_text(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]: ...


class OpenAICompatibleChatAdapter:
    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: SecretStr | None,
        model: str | None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model = model

    def ensure_configured(self) -> None:
        if self._base_url is None or self._api_key is None or self._model is None:
            raise ChatConfigurationError("Chat configuration is required before accepting a turn")

    async def complete_text(self, messages: Sequence[ChatMessage]) -> str:
        payload = await self._request(messages, stream=False, json_mode=False)
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatError("Chat response did not contain text") from exc
        if not isinstance(content, str) or not content:
            raise ChatError("Chat response text was empty")
        return content

    async def complete_json(self, messages: Sequence[ChatMessage]) -> dict[str, Any]:
        content = await self.complete_text_json_mode(messages)
        try:
            parsed: object = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ChatError("Structured Chat response was invalid") from exc
        if not isinstance(parsed, dict):
            raise ChatError("Structured Chat response was not an object")
        return cast(dict[str, Any], parsed)

    async def complete_text_json_mode(self, messages: Sequence[ChatMessage]) -> str:
        payload = await self._request(messages, stream=False, json_mode=True)
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatError("Chat response did not contain structured text") from exc
        if not isinstance(content, str) or not content:
            raise ChatError("Structured Chat response was empty")
        return content

    async def stream_text(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:
        self.ensure_configured()
        assert self._base_url is not None
        assert self._api_key is not None
        assert self._model is not None
        body = self._body(messages, stream=True, json_mode=False)
        received_content = False
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                    json=body,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            if not received_content:
                                raise ChatError("Chat stream did not contain answer text")
                            return
                        raw_payload: object = json.loads(data)
                        if not isinstance(raw_payload, dict):
                            continue
                        payload_dict = cast(dict[str, object], raw_payload)
                        raw_choices = payload_dict.get("choices")
                        if not isinstance(raw_choices, list) or not raw_choices:
                            continue
                        choices = cast(list[object], raw_choices)
                        choice = choices[0]
                        if not isinstance(choice, dict):
                            continue
                        choice_dict = cast(dict[str, object], choice)
                        delta_payload: object = choice_dict.get("delta")
                        if not isinstance(delta_payload, dict):
                            continue
                        delta = cast(dict[str, object], delta_payload).get("content")
                        if isinstance(delta, str) and delta:
                            received_content = True
                            yield delta
                    raise ChatError("Chat stream ended without a terminal marker")
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ChatError("Chat stream failed") from exc

    async def _request(
        self,
        messages: Sequence[ChatMessage],
        *,
        stream: bool,
        json_mode: bool,
    ) -> dict[str, Any]:
        self.ensure_configured()
        assert self._base_url is not None
        assert self._api_key is not None
        body = self._body(messages, stream=stream, json_mode=json_mode)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self._base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                    json=body,
                )
                response.raise_for_status()
                raw_payload: object = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChatError("Chat request failed") from exc
        if not isinstance(raw_payload, dict):
            raise ChatError("Chat response was not an object")
        return cast(dict[str, Any], raw_payload)

    def _body(
        self,
        messages: Sequence[ChatMessage],
        *,
        stream: bool,
        json_mode: bool,
    ) -> dict[str, Any]:
        assert self._model is not None
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "stream": stream,
            "enable_thinking": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body
