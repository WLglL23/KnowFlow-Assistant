from __future__ import annotations

import hashlib
from typing import Any

from knowflow.knowledge.answering.outcomes import AnsweringOutcome
from knowflow.knowledge.answering.retrieval import EvidenceTokenizerV1


class SafeObservationProjector:
    """Project one in-memory outcome into the redacted evaluator contract."""

    def project(
        self, outcome: AnsweringOutcome, *, failure_code: str | None = None
    ) -> dict[str, Any]:
        failures = [
            code
            for code in [
                failure_code,
                *(item.failure_code for item in outcome.subquestions),
            ]
            if isinstance(code, str)
        ]
        retrieval_active = outcome.route == "RETRIEVAL"
        return {
            "run_status": "FAILED" if failure_code is not None else outcome.run_status,
            "failure_code": failures[0] if failures else None,
            "latency_ms_total": outcome.latency_ms_total,
            "model_call_count": outcome.model_call_count,
            "retrieval_call_count": outcome.query_count if retrieval_active else 0,
            "citation_total": len(outcome.citations),
            # Citations are global, de-duplicated output records. Counting per
            # subquestion double-counts an intentionally shared citation.
            "valid_citation_count": len(outcome.citations),
            "answer_with_valid_citation": any(
                item.status == "ANSWERED" and item.citation_integrity is True
                for item in outcome.subquestions
            ),
            "subquestions": [
                {
                    "subquestion_id_hash": item.subquestion_id_hash,
                    "retrieval_round_count": item.retrieval_round_count,
                    "initial_candidate_count": item.initial_candidate_count,
                    "rerank_candidate_count": item.rerank_candidate_count,
                    "selected_evidence_count": len(item.evidence),
                    "selected_evidence_token_count": item.selected_evidence_token_count,
                    "final_evidence_count": len(item.used_evidence),
                    "final_evidence_token_count": sum(
                        EvidenceTokenizerV1.count(evidence.text)
                        for evidence in item.used_evidence
                    ),
                    "input_reference_page_hit": None,
                    "initial_candidate_page_hashes": list(
                        item.initial_candidate_page_hashes
                    ),
                    "rerank_candidate_page_hashes": list(
                        item.rerank_candidate_page_hashes
                    ),
                    "verify_input_page_hashes": sorted(item.verify_input_page_hashes),
                    "final_evidence_page_hashes": sorted(
                        {
                            hashlib.sha256(
                                f"{evidence.source_id}:{page}".encode()
                            ).hexdigest()
                            for evidence in item.used_evidence
                            for page in evidence.pages
                        }
                    ),
                    "verify_label_count": item.label_count,
                    "verify_missing_fact_count": len(item.missing_facts),
                    "has_supplemental_query": item.has_supplemental_query,
                    "second_round_identity_changed": item.second_round_identity_changed,
                    "second_round_useful": item.second_round_useful,
                    "answer_executed": item.answer_executed,
                    "citation_presence": item.citation_presence,
                    "citation_integrity": item.citation_integrity,
                }
                for item in outcome.subquestions
            ],
            "retrieval": (
                {
                    "query_count": outcome.query_count,
                    "retrieval_round_count": outcome.retrieval_round_count,
                    "initial_candidate_count": sum(
                        item.initial_candidate_count for item in outcome.subquestions
                    ),
                    "rerank_candidate_count": sum(
                        item.rerank_candidate_count for item in outcome.subquestions
                    ),
                    "selected_evidence_count": len(
                        {
                            (item.source_id, item.chunk_id)
                            for state in outcome.subquestions
                            for item in state.evidence
                        }
                    ),
                    "selected_evidence_token_count": outcome.selected_evidence_token_count,
                    "duplicate_chunk_count": outcome.duplicate_chunk_count,
                    "source_type_count": outcome.source_type_count,
                }
                if retrieval_active
                else {}
            ),
            # Run and visible-character limits apply to every route. Evidence,
            # budget, and source-scope limits are retrieval-only and remain
            # unknown for GENERAL rather than being treated as violations.
            "limits": {
                "run_limit_compliance": outcome.limit_compliance.run,
                "visible_character_limit_compliance": (
                    outcome.limit_compliance.visible_characters
                ),
                "budget_limit_compliance": (
                    outcome.limit_compliance.budget if retrieval_active else None
                ),
                "source_scope_compliance": (
                    outcome.limit_compliance.source_scope if retrieval_active else None
                ),
                "unsafe_persisted_content_detected": False,
            },
            "failures": list(dict.fromkeys(failures)),
        }
