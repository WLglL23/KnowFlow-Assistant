"""Shared Docling chunk text and provenance for both ingestion paths."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from docling_core.transforms.chunker.hybrid_chunker import HybridChunker


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    pages: list[int]
    text: str
    document_title: str | None
    headings: list[str]


def chunk_document(document: Any) -> list[DocumentChunk]:
    titles = [
        item.text.strip()
        for item in getattr(document, 'texts', [])
        if getattr(item, 'label', None) == 'title' and getattr(item, 'text', '').strip()
    ]
    # Docling can classify a cover title as section_header. Use its document
    # name as a display fallback, never promote an arbitrary chapter to title.
    name = getattr(document, 'name', None)
    title = titles[0] if titles else (name.strip() if isinstance(name, str) else None)
    chunks: list[DocumentChunk] = []
    for chunk in cast(Any, HybridChunker().chunk(document)):
        text = chunk.text.strip()
        pages = sorted({
            int(p.page_no) for item in chunk.meta.doc_items for p in item.prov
            if p.page_no is not None
        })
        if text and pages:
            headings = cast(list[str], chunk.meta.headings or [])
            chunks.append(DocumentChunk(
                pages, text, title,
                [h.strip() for h in headings if h.strip()],
            ))
    if not chunks:
        raise ValueError('PAGE_NUMBERS_MISSING: Docling produced no chunk with page provenance')
    return chunks
