"""Persisted diagnostics shared with earlier retrieval policies."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from knowflow.knowledge.answering.outcomes import SubquestionOutcome

LEGACY_RETRIEVAL_STAGE = "RETRIEVE_2"


class LegacyRetrievalFields(TypedDict):
    has_supplemental_query: bool
    second_round_identity_changed: bool | None
    second_round_useful: bool | None


class LegacyRetrievalTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_supplemental_query: bool = False
    second_round_identity_changed: bool | None = None
    second_round_useful: bool | None = None


def legacy_retrieval_fields(outcome: SubquestionOutcome) -> LegacyRetrievalFields:
    return {
        "has_supplemental_query": outcome.has_supplemental_query,
        "second_round_identity_changed": outcome.second_round_identity_changed,
        "second_round_useful": outcome.second_round_useful,
    }
