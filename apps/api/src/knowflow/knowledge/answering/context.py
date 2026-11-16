from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from knowflow.knowledge.answering.chat import ChatMessage


@dataclass(frozen=True, slots=True)
class HistoricalMessage:
    id: str
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class PreparedContext:
    summary_text: str | None
    recent_messages: tuple[HistoricalMessage, ...]
    summary_candidates: tuple[HistoricalMessage, ...]

    def chat_messages(self, current_user_content: str) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        if self.summary_text:
            messages.append(ChatMessage("system", f"会话累计摘要：{self.summary_text}"))
        for message in self.recent_messages:
            role = "user" if message.role == "USER" else "assistant"
            messages.append(ChatMessage(role, message.content))
        messages.append(ChatMessage("user", current_user_content))
        return messages


def prepare_context(
    history: Sequence[HistoricalMessage],
    *,
    summary_text: str | None,
) -> PreparedContext:
    if len(history) > 20:
        return PreparedContext(
            summary_text=summary_text,
            recent_messages=tuple(history[-12:]),
            summary_candidates=tuple(history[:-12]),
        )
    return PreparedContext(
        summary_text=summary_text,
        recent_messages=tuple(history[-12:]),
        summary_candidates=(),
    )


def summary_messages(context: PreparedContext) -> list[ChatMessage]:
    source: list[ChatMessage] = [
        ChatMessage(
            "system",
            "生成简洁的累计会话摘要。只返回摘要正文，不返回解释。",
        )
    ]
    if context.summary_text:
        source.append(ChatMessage("system", f"旧摘要：{context.summary_text}"))
    for message in context.summary_candidates:
        role = "user" if message.role == "USER" else "assistant"
        source.append(ChatMessage(role, message.content))
    return source
