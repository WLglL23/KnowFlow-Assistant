from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunLimits:
    deadline_seconds: int
    max_subquestions: int = 5
    max_retrieval_rounds: int = 2
    max_actual_queries: int = 10
    max_evidence_chunks: int = 8
    max_evidence_tokens: int = 6000
    max_retries: int = 1
    max_verify_thinking_tokens: int = 2048
    max_answer_thinking_tokens: int = 2048
    max_visible_characters: int = 8000


@dataclass(frozen=True, slots=True)
class RunPolicy:
    policy_version: str
    trace_version: str
    limits: RunLimits


def core_experimental_policy(deadline_seconds: int) -> RunPolicy:
    return RunPolicy(
        policy_version="core-exp-1",
        trace_version="run-trace-v2",
        limits=RunLimits(deadline_seconds=deadline_seconds),
    )
