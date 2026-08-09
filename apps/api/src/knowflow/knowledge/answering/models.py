from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from knowflow.app.database import Base


class MessageRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class MessageStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class AnswerRoute(StrEnum):
    GENERAL = "GENERAL"
    RETRIEVAL = "RETRIEVAL"


class StageName(StrEnum):
    UNDERSTAND = "UNDERSTAND"
    PLAN = "PLAN"
    RETRIEVE_1 = "RETRIEVE_1"
    VERIFY = "VERIFY"
    RETRIEVE_2 = "RETRIEVE_2"
    GENERATE = "GENERATE"


class StageStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(37), ForeignKey("conversations.id"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, name="message_role", native_enum=True), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name="message_status", native_enum=True), nullable=False
    )
    citations_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(
        String(37), ForeignKey("conversations.id"), nullable=False
    )
    user_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id"), nullable=False
    )
    assistant_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id"), nullable=False
    )
    client_request_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status", native_enum=True), nullable=False
    )
    route: Mapped[AnswerRoute | None] = mapped_column(
        Enum(AnswerRoute, name="answer_route", native_enum=True), nullable=True
    )
    source_scope_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    trace_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)


class AgentRunStage(Base):
    __tablename__ = "agent_run_stages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id"), nullable=False)
    stage: Mapped[StageName] = mapped_column(
        Enum(StageName, name="agent_stage", native_enum=True), nullable=False
    )
    status: Mapped[StageStatus] = mapped_column(
        Enum(StageStatus, name="agent_stage_status", native_enum=True), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    summary_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
