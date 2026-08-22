from __future__ import annotations

from typing import Any

from knowflow.knowledge.answering.outcomes import AnsweringOutcome


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
            "coverage_observed": (
                outcome.coverage if outcome.route == "RETRIEVAL" else None
            ),
            "subquestions": [
                {
                    "subquestion_id_hash": item.subquestion_id_hash,
                    "retrieval_round_count": item.retrieval_round_count,
                    "initial_candidate_count": item.initial_candidate_count,
                    "rerank_candidate_count": item.rerank_candidate_count,
                    "selected_evidence_count": len(item.evidence),
                    "selected_evidence_token_count": item.selected_evidence_token_count,
                    "input_reference_page_hit": None,
                    "verify_input_page_hashes": sorted(item.verify_input_page_hashes),
                    "observed_sufficiency": (
                        "SUFFICIENT"
                        if item.status == "ANSWERED"
                        else "INSUFFICIENT"
                        if item.status == "INSUFFICIENT"
                        else None
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
            "limits": (
                {
                    "run_limit_compliance": outcome.limit_compliance.run,
                    "visible_character_limit_compliance": (
                        outcome.limit_compliance.visible_characters
                    ),
                    "budget_limit_compliance": outcome.limit_compliance.budget,
                    "source_scope_compliance": outcome.limit_compliance.source_scope,
                }
                if retrieval_active
                else {}
            ),
            "failures": list(dict.fromkeys(failures)),
        }
