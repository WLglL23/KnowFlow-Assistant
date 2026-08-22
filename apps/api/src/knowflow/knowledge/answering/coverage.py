from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from knowflow.knowledge.answering.outcomes import SubquestionOutcome

Coverage = Literal["FULL", "PARTIAL", "NONE"]


def aggregate_coverage(outcomes: Iterable[SubquestionOutcome]) -> Coverage:
    """Derive run coverage from local answer outcomes, never from model labels."""
    values = list(outcomes)
    answered = sum(item.status == "ANSWERED" for item in values)
    if values and answered == len(values):
        return "FULL"
    if answered:
        return "PARTIAL"
    return "NONE"

