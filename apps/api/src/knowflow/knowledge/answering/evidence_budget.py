from __future__ import annotations

import re
from dataclasses import dataclass

from knowflow.knowledge.answering.retrieval import Evidence, EvidenceTokenizerV1


def prioritize_candidates(query: str, candidates: list[Evidence]) -> list[Evidence]:
    """Keep semantic rerank order while preferring a new page over duplicate pages."""
    del query  # Semantic relevance is already represented by the rerank ordering.
    ordered: list[Evidence] = []
    remaining = list(candidates)
    seen_pages: set[tuple[str, int]] = set()
    while remaining:
        novel_index = next(
            (
                index
                for index, item in enumerate(remaining)
                if any((item.source_id, page) not in seen_pages for page in item.pages)
            ),
            None,
        )
        selected_index = 0 if novel_index is None else novel_index
        item = remaining.pop(selected_index)
        ordered.append(item)
        seen_pages.update((item.source_id, page) for page in item.pages)
    return ordered


def promote_query_anchors(
    query: str, candidates: list[Evidence], *, window: int = 4
) -> list[Evidence]:
    """Keep rerank order while rescuing explicit acronym anchors from cutoff.

    Rerank providers may place the one chunk containing an explicit product or
    protocol anchor just beyond the per-question evidence window.  Promoting
    only query tokens written as an acronym keeps this deterministic and
    narrow; ordinary semantic ordering remains unchanged for normal prose.
    """
    anchors = {
        value.lower()
        for value in re.findall(
            r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_-]{2,}(?![A-Za-z0-9])", query
        )
    }
    if not anchors or window <= 0 or len(candidates) <= window:
        return candidates
    head = candidates[:window]
    if any(
        any(anchor in candidate.text.lower() for anchor in anchors) for candidate in head
    ):
        return candidates
    rescue_index = next(
        (
            index
            for index, candidate in enumerate(candidates[window:], start=window)
            if any(anchor in candidate.text.lower() for anchor in anchors)
        ),
        None,
    )
    if rescue_index is None:
        # Rerank text can contain CJK prose where the Latin acronym is split
        # or omitted from an individual chunk. Use meaningful CJK runs as a
        # narrow lexical fallback, while leaving normal semantic order intact.
        terms = {
            value
            for run in re.findall(r"[\u3400-\u9fff]{2,}", query)
            for value in re.findall(r"[\u3400-\u9fff]{2,4}", run)
            if value not in {"什么是", "为什么", "如何", "基于", "资料", "问题"}
        }
        terms.update(
            term
            for anchor in anchors
            for term in {"检索", "增强", "生成"}
            if anchor == "rag"
        )
        if terms:
            rescue_index = next(
                (
                    index
                    for index, candidate in enumerate(candidates[window:], start=window)
                    if any(term in candidate.text for term in terms)
                ),
                None,
            )
    if rescue_index is None:
        return candidates
    rescue = candidates[rescue_index]
    return [rescue, *candidates[:rescue_index], *candidates[rescue_index + 1 :]]


@dataclass(frozen=True, slots=True)
class EvidenceBudget:
    global_chunks: int
    global_tokens: int
    per_subquestion_chunks: int
    per_subquestion_tokens: int


@dataclass(frozen=True, slots=True)
class EvidenceAllocation:
    unique_evidence: list[Evidence]
    by_subquestion: list[list[Evidence]]
    total_tokens: int
    duplicate_chunk_count: int


def allocate_evidence(
    ranked_candidates: list[list[Evidence]], budget: EvidenceBudget
) -> EvidenceAllocation:
    """Allocate PLAN-ordered evidence with one pass of coverage before round-robin fill."""
    selected: list[list[Evidence]] = [[] for _ in ranked_candidates]
    selected_keys_by_question: list[set[tuple[str, str]]] = [set() for _ in ranked_candidates]
    unique: list[Evidence] = []
    unique_keys: set[tuple[str, str]] = set()
    per_question_tokens = [0 for _ in ranked_candidates]
    total_tokens = 0
    duplicate_count = 0

    def add(question_index: int, candidate: Evidence) -> bool:
        nonlocal total_tokens, duplicate_count
        key = (candidate.source_id, candidate.chunk_id)
        if key in selected_keys_by_question[question_index]:
            return False
        candidate_tokens = EvidenceTokenizerV1.count(candidate.text)
        if len(selected[question_index]) >= budget.per_subquestion_chunks:
            return False
        if per_question_tokens[question_index] + candidate_tokens > budget.per_subquestion_tokens:
            return False
        is_new = key not in unique_keys
        if is_new and len(unique) >= budget.global_chunks:
            return False
        if is_new and total_tokens + candidate_tokens > budget.global_tokens:
            return False
        selected[question_index].append(candidate)
        selected_keys_by_question[question_index].add(key)
        per_question_tokens[question_index] += candidate_tokens
        if is_new:
            unique.append(candidate)
            unique_keys.add(key)
            total_tokens += candidate_tokens
        else:
            duplicate_count += 1
        return True

    for question_index, candidates in enumerate(ranked_candidates):
        for candidate in candidates:
            if add(question_index, candidate):
                break

    made_progress = True
    while made_progress:
        made_progress = False
        for question_index, candidates in enumerate(ranked_candidates):
            for candidate in candidates:
                if add(question_index, candidate):
                    made_progress = True
                    break

    return EvidenceAllocation(
        unique_evidence=unique,
        by_subquestion=selected,
        total_tokens=total_tokens,
        duplicate_chunk_count=duplicate_count,
    )
