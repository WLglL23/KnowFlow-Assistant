from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from knowflow.knowledge.answering.outcomes import SubquestionOutcome
from knowflow.knowledge.answering.retrieval import Evidence

Retrieve = Callable[
    [str],
    Awaitable[
        tuple[list[Evidence], int, int]
        | tuple[list[Evidence], int, int, list[str]]
    ],
]
Answer = Callable[[str, list[Evidence]], Awaitable["SubquestionAnswer"]]


@dataclass(frozen=True, slots=True)
class SubquestionAnswer:
    status: Literal["ANSWERED", "INSUFFICIENT"]
    answer: str
    labels: list[str]
    missing_facts: list[str]


@dataclass(frozen=True, slots=True)
class RetrievedSubquestion:
    outcome: SubquestionOutcome
    candidates: list[Evidence]


class SubquestionRunner:
    """Own one subquestion's retrieval and answer validation."""

    def __init__(self, retrieve: Retrieve | None, answer: Answer | None = None) -> None:
        self._retrieve = retrieve
        self._answer = answer

    def new_outcome(self, query: str) -> SubquestionOutcome:
        import hashlib

        return SubquestionOutcome(
            subquestion_id_hash=hashlib.sha256(query.encode("utf-8")).hexdigest(),
            query=query,
            status="INSUFFICIENT",
            answer=None,
            evidence=[],
            citations=[],
            failure_code=None,
            answer_executed=False,
        )

    async def retrieve(self, query: str) -> RetrievedSubquestion:
        import hashlib

        if self._retrieve is None:
            raise RuntimeError("retrieve callback is required")
        retrieved = await self._retrieve(query)
        if len(retrieved) == 3:
            candidates, initial_count, rerank_count = retrieved
            initial_page_hashes = [
                hashlib.sha256(f"{item.source_id}:{page}".encode()).hexdigest()
                for item in candidates
                for page in item.pages
            ]
        else:
            candidates, initial_count, rerank_count, initial_page_hashes = retrieved
        outcome = self.new_outcome(query)
        outcome.retrieval_round_count = 1
        outcome.initial_candidate_count = initial_count
        outcome.rerank_candidate_count = rerank_count
        outcome.initial_candidate_page_hashes = list(dict.fromkeys(initial_page_hashes))
        outcome.rerank_candidate_page_hashes = list(
            dict.fromkeys(
                hashlib.sha256(f"{item.source_id}:{page}".encode()).hexdigest()
                for item in candidates
                for page in item.pages
            )
        )
        return RetrievedSubquestion(outcome, candidates)

    async def answer(self, outcome: SubquestionOutcome) -> list[Evidence] | None:
        """Run and validate this question's one structured ANSWER call."""
        if outcome.status == "FAILED":
            return None
        if not outcome.evidence:
            outcome.status = "INSUFFICIENT"
            outcome.missing_facts = ["足以回答该问题的 PDF 证据"]
            return []
        if self._answer is None:
            raise RuntimeError("answer callback is required")
        result = await self._answer(outcome.query, outcome.evidence)
        if result.status == "INSUFFICIENT":
            outcome.status = "INSUFFICIENT"
            outcome.missing_facts = list(result.missing_facts)
            return []
        return self.apply_answer(outcome, result)

    def apply_answer(
        self, outcome: SubquestionOutcome, result: SubquestionAnswer
    ) -> list[Evidence] | None:
        if result.status == "INSUFFICIENT":
            outcome.status = "INSUFFICIENT"
            outcome.missing_facts = list(result.missing_facts)
            return []
        if not result.answer.strip() or not result.labels:
            outcome.status = "FAILED"
            outcome.failure_code = "MODEL_FAILED"
            return None
        evidence_by_label = {
            f"E{index + 1}": item for index, item in enumerate(outcome.evidence)
        }
        if not set(result.labels).issubset(evidence_by_label):
            outcome.status = "FAILED"
            outcome.failure_code = "MODEL_FAILED"
            return None
        used = [evidence_by_label[label] for label in dict.fromkeys(result.labels)]
        outcome.status = "ANSWERED"
        outcome.answer = result.answer.strip()
        outcome.answer_executed = True
        outcome.used_evidence = used
        outcome.label_count = len(result.labels)
        outcome.citation_presence = bool(used)
        outcome.citation_integrity = bool(used)
        return used
