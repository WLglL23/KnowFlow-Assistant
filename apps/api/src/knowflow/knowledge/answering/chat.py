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
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ChatUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class ChatTextResult:
    content: str
    usage: ChatUsage | None


@dataclass(frozen=True, slots=True)
class ChatJsonResult:
    payload: dict[str, Any]
    usage: ChatUsage | None


@dataclass(frozen=True, slots=True)
class ChatTextDelta:
    delta: str


@dataclass(frozen=True, slots=True)
class ChatStreamUsage:
    usage: ChatUsage


type ChatStreamEvent = ChatTextDelta | ChatStreamUsage


class ChatClient(Protocol):
    def ensure_configured(self) -> None: ...

    async def complete_text(
        self, messages: Sequence[ChatMessage], *, timeout_seconds: float
    ) -> ChatTextResult: ...

    async def complete_json(
        self,
        messages: Sequence[ChatMessage],
        *,
        timeout_seconds: float,
        reasoning_budget_tokens: int | None = None,
    ) -> ChatJsonResult: ...

    def stream_text(
        self,
        messages: Sequence[ChatMessage],
        *,
        timeout_seconds: float,
        reasoning_budget_tokens: int | None = None,
    ) -> AsyncIterator[ChatStreamEvent]: ...


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

    async def complete_text(
        self, messages: Sequence[ChatMessage], *, timeout_seconds: float
    ) -> ChatTextResult:
        payload = await self._request(
            messages,
            stream=False,
            json_mode=False,
            timeout_seconds=timeout_seconds,
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatError("Chat response did not contain text", retryable=True) from exc
        if not isinstance(content, str) or not content:
            raise ChatError("Chat response text was empty", retryable=True)
        return ChatTextResult(content=content, usage=self._usage(payload))

    async def complete_json(
        self,
        messages: Sequence[ChatMessage],
        *,
        timeout_seconds: float,
        reasoning_budget_tokens: int | None = None,
    ) -> ChatJsonResult:
        result = await self._complete_text_json_mode(
            messages,
            timeout_seconds=timeout_seconds,
            reasoning_budget_tokens=reasoning_budget_tokens,
        )
        try:
            parsed: object = json.loads(result.content)
        except json.JSONDecodeError as exc:
            raise ChatError("Structured Chat response was invalid", retryable=True) from exc
        if not isinstance(parsed, dict):
            raise ChatError("Structured Chat response was not an object", retryable=True)
        return ChatJsonResult(payload=cast(dict[str, Any], parsed), usage=result.usage)

    async def _complete_text_json_mode(
        self,
        messages: Sequence[ChatMessage],
        *,
        timeout_seconds: float,
        reasoning_budget_tokens: int | None,
    ) -> ChatTextResult:
        payload = await self._request(
            messages,
            stream=False,
            json_mode=True,
            timeout_seconds=timeout_seconds,
            reasoning_budget_tokens=reasoning_budget_tokens,
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatError(
                "Chat response did not contain structured text", retryable=True
            ) from exc
        if not isinstance(content, str) or not content:
            raise ChatError("Structured Chat response was empty", retryable=True)
        return ChatTextResult(content=content, usage=self._usage(payload))

    async def stream_text(
        self,
        messages: Sequence[ChatMessage],
        *,
        timeout_seconds: float,
        reasoning_budget_tokens: int | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        self.ensure_configured()
        assert self._base_url is not None
        assert self._api_key is not None
        body = self._body(
            messages,
            stream=True,
            json_mode=False,
            reasoning_budget_tokens=reasoning_budget_tokens,
        )
        received_content = False
        try:
            async with httpx.AsyncClient(timeout=max(timeout_seconds, 0.001)) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                    json=body,
                ) as response:
                    self._raise_for_status(response)
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            if not received_content:
                                raise ChatError(
                                    "Chat stream did not contain answer text",
                                    retryable=True,
                                )
                            return
                        raw_payload: object = json.loads(data)
                        if not isinstance(raw_payload, dict):
                            continue
                        payload = cast(dict[str, Any], raw_payload)
                        usage = self._usage(payload)
                        if usage is not None:
                            yield ChatStreamUsage(usage)
                        raw_choices: object = payload.get("choices")
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
                        delta_dict = cast(dict[str, object], delta_payload)
                        delta: object = delta_dict.get("content")
                        if isinstance(delta, str) and delta:
                            received_content = True
                            yield ChatTextDelta(delta)
                    raise ChatError("Chat stream ended without a terminal marker", retryable=True)
        except ChatError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ChatError(
                "Chat stream failed",
                retryable=self._retryable_status(exc.response.status_code),
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ChatError("Chat stream failed", retryable=True) from exc
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ChatError("Chat stream failed", retryable=True) from exc

    async def _request(
        self,
        messages: Sequence[ChatMessage],
        *,
        stream: bool,
        json_mode: bool,
        timeout_seconds: float,
        reasoning_budget_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_configured()
        assert self._base_url is not None
        assert self._api_key is not None
        body = self._body(
            messages,
            stream=stream,
            json_mode=json_mode,
            reasoning_budget_tokens=reasoning_budget_tokens,
        )
        try:
            async with httpx.AsyncClient(timeout=max(timeout_seconds, 0.001)) as client:
                response = await client.post(
                    f"{self._base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                    json=body,
                )
                self._raise_for_status(response)
                raw_payload: object = response.json()
        except ChatError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ChatError(
                "Chat request failed",
                retryable=self._retryable_status(exc.response.status_code),
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ChatError("Chat request failed", retryable=True) from exc
        except ValueError as exc:
            raise ChatError("Chat response was invalid", retryable=True) from exc
        if not isinstance(raw_payload, dict):
            raise ChatError("Chat response was not an object", retryable=True)
        return cast(dict[str, Any], raw_payload)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        response.raise_for_status()

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code in {408, 429} or status_code >= 500

    @staticmethod
    def _usage(payload: dict[str, Any]) -> ChatUsage | None:
        raw: object = payload.get("usage")
        if not isinstance(raw, dict):
            return None
        usage_dict = cast(dict[str, object], raw)
        prompt: object = usage_dict.get("prompt_tokens")
        completion: object = usage_dict.get("completion_tokens")
        total: object = usage_dict.get("total_tokens")
        if not all(isinstance(value, int) and value >= 0 for value in (prompt, completion, total)):
            return None
        return ChatUsage(
            prompt_tokens=cast(int, prompt),
            completion_tokens=cast(int, completion),
            total_tokens=cast(int, total),
        )

    def _body(
        self,
        messages: Sequence[ChatMessage],
        *,
        stream: bool,
        json_mode: bool,
        reasoning_budget_tokens: int | None = None,
    ) -> dict[str, Any]:
        assert self._model is not None
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "stream": stream,
            "enable_thinking": reasoning_budget_tokens is not None,
        }
        if reasoning_budget_tokens is not None:
            if reasoning_budget_tokens <= 0:
                raise ChatConfigurationError("Reasoning budget must be positive")
            body["thinking_budget"] = reasoning_budget_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body
