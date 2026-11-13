from __future__ import annotations

import hashlib
from typing import cast

from knowflow.knowledge.answering.evidence_budget import EvidenceBudget, allocate_evidence
from knowflow.knowledge.answering.execution import RunExecutionContext, RunExecutionError
from knowflow.knowledge.answering.outcomes import SubquestionOutcome
from knowflow.knowledge.answering.policy import RunPolicy
from knowflow.knowledge.answering.retrieval import (
    AttachmentRetrieval,
    Evidence,
    EvidenceTokenizerV1,
    RetrievalError,
)


class RetrievalPipeline:
    def __init__(self, retrieval: AttachmentRetrieval, policy: RunPolicy) -> None:
        self._retrieval = retrieval
        self._policy = policy

    async def query(
        self,
        query: str,
        source_filenames: dict[str, str],
        context: RunExecutionContext,
    ) -> tuple[list[Evidence], int, int, list[str]]:
        if context.actual_queries >= context.policy.limits.max_actual_queries:
            raise RunExecutionError("RUN_LIMIT_EXCEEDED")
        context.actual_queries += 1
        while True:
            try:
                vector = await self._retrieval.embed(
                    query, timeout_seconds=context.timeout_seconds()
                )
                break
            except RetrievalError as exc:
                if not (exc.retryable and context.consume_retry()):
                    raise RunExecutionError("RETRIEVAL_FAILED") from exc
        try:
            evidence = await self._retrieval.hybrid(
                query, vector, source_filenames, timeout_seconds=context.timeout_seconds()
            )
        except RetrievalError as exc:
            raise RunExecutionError("RETRIEVAL_FAILED") from exc
        while True:
            try:
                initial_page_hashes = list(
                    dict.fromkeys(
                        hashlib.sha256(f"{item.source_id}:{page}".encode()).hexdigest()
                        for item in evidence
                        for page in item.pages
                    )
                )
                reranked = await self._retrieval.rerank(
                    query, evidence, timeout_seconds=context.timeout_seconds()
                )
                return reranked, len(evidence), len(reranked), initial_page_hashes
            except RetrievalError as exc:
                if not (exc.retryable and context.consume_retry()):
                    raise RunExecutionError("RETRIEVAL_FAILED") from exc

    @staticmethod
    def token_count(item: Evidence) -> int:
        from knowflow.knowledge.answering.retrieval import EvidenceTokenizerV1

        return EvidenceTokenizerV1.count(item.text)

    def allocate(self, context: RunExecutionContext) -> None:
        limits = self._policy.limits
        allocation = allocate_evidence(
            context.candidate_sets,
            EvidenceBudget(
                global_chunks=limits.max_evidence_chunks,
                global_tokens=limits.max_evidence_tokens,
                per_subquestion_chunks=limits.max_subquestion_evidence_chunks,
                per_subquestion_tokens=limits.max_subquestion_evidence_tokens,
            ),
        )
        for state, evidence in zip(context.subquestions, allocation.by_subquestion, strict=True):
            state.evidence = evidence
            state.selected_evidence_token_count = sum(
                EvidenceTokenizerV1.count(item.text) for item in evidence
            )
        context.evidence = allocation.unique_evidence
        context.evidence_token_count = allocation.total_tokens
        context.duplicate_evidence_count = allocation.duplicate_chunk_count

    @staticmethod
    def unique_evidence(states: list[SubquestionOutcome]) -> list[Evidence]:
        result: list[Evidence] = []
        seen: set[tuple[str, str]] = set()
        for state in states:
            for item in state.evidence:
                key = (item.source_id, item.chunk_id)
                if key not in seen:
                    seen.add(key)
                    result.append(item)
        return result

    @staticmethod
    def valid_evidence(
        evidence: list[Evidence], source_filenames: dict[str, str], context: RunExecutionContext
    ) -> bool:
        if any(
            not item.text.strip() or item.source_id not in source_filenames or not item.pages
            for item in evidence
        ):
            context.source_scope_compliant = False
            return False
        return True

    @staticmethod
    def citation_markers(evidence: list[Evidence], citations: list[dict[str, object]]) -> str:
        indexes: list[str] = []
        for item in evidence:
            for index, citation in enumerate(citations):
                if citation.get("source_id") == item.source_id:
                    indexes.append(str(index))
                    break
        return "".join(f"[[citation:{index}]]" for index in dict.fromkeys(indexes))

    @staticmethod
    def citations(evidence: list[Evidence]) -> list[dict[str, object]]:
        citations: list[dict[str, object]] = []
        for item in evidence:
            current = next(
                (citation for citation in citations if citation["source_id"] == item.source_id),
                None,
            )
            if current is None:
                citations.append(
                    {
                        "source_type": item.source_type,
                        "source_id": item.source_id,
                        "filename": item.filename,
                        "pages": list(item.pages[:5]),
                    }
                )
            else:
                raw_pages = current.get("pages")
                pages = (
                    [page for page in cast(list[object], raw_pages) if isinstance(page, int)]
                    if isinstance(raw_pages, list)
                    else []
                )
                current["pages"] = list(dict.fromkeys(pages + list(item.pages)))[:5]
        return citations
