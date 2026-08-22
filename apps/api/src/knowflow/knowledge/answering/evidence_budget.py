from __future__ import annotations

from dataclasses import dataclass

from knowflow.knowledge.answering.retrieval import Evidence, EvidenceTokenizerV1


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
