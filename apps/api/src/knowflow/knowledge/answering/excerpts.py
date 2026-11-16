from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from knowflow.knowledge.answering.retrieval import Evidence


@dataclass(frozen=True, slots=True)
class EvidenceExcerpt:
    evidence_label: str
    quote: str


class InvalidExcerpt(ValueError):
    """A model selection cannot be located in its own evidence."""


def _match_text(text: str) -> tuple[str, list[int]]:
    characters: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", character):
            if not normalized.isspace():
                characters.append(normalized)
                positions.append(index)
    return "".join(characters), positions


def extract_answer(
    excerpts: list[EvidenceExcerpt], evidence: list[Evidence]
) -> tuple[str, list[Evidence]]:
    """Resolve selections to source slices; never publish the model's quote itself."""
    if not excerpts or len(excerpts) > 20:
        raise InvalidExcerpt("an answered question requires bounded source excerpts")
    evidence_by_label = {f"E{index + 1}": item for index, item in enumerate(evidence)}
    fragments: list[str] = []
    used: list[Evidence] = []
    seen: set[tuple[str, int, int]] = set()
    for excerpt in excerpts:
        item = evidence_by_label.get(excerpt.evidence_label)
        if item is None:
            raise InvalidExcerpt("excerpt label is outside this question's evidence")
        quote, _ = _match_text(excerpt.quote)
        source, positions = _match_text(item.text)
        start = source.find(quote) if quote else -1
        if start < 0:
            raise InvalidExcerpt("excerpt is not present in the selected evidence")
        first, last = positions[start], positions[start + len(quote) - 1] + 1
        fragment = item.text[first:last]
        if "[[citation:" in fragment:
            raise InvalidExcerpt("excerpt contains a reserved citation marker")
        identity = (excerpt.evidence_label, first, last)
        if identity in seen:
            continue
        seen.add(identity)
        fragments.append(fragment)
        if item not in used:
            used.append(item)
    return "\n\n".join(fragments), used
