from __future__ import annotations

from typing import Literal
from urllib.parse import quote

import httpx

from knowflow.app.config import Settings
from knowflow.knowledge.documents.chunks import DocumentChunk


async def embed_documents(settings: Settings, texts: list[str]) -> list[list[float]]:
    if not settings.embedding_base_url:
        raise RuntimeError("embedding is not configured")
    headers: dict[str, str] = {}
    if settings.embedding_api_key:
        headers["Authorization"] = f"Bearer {settings.embedding_api_key.get_secret_value()}"
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for start in range(0, len(texts), 20):
            batch = texts[start : start + 20]
            response = await client.post(
                f"{settings.embedding_base_url.rstrip('/')}/embeddings",
                headers=headers,
                json={"model": settings.embedding_model, "input": batch},
            )
            response.raise_for_status()
            vectors.extend(item["embedding"] for item in response.json()["data"])
    if len(vectors) != len(texts) or any(
        len(vector) != settings.embedding_dimensions for vector in vectors
    ):
        raise RuntimeError("embedding dimension mismatch")
    return vectors


async def ensure_chunk_index(settings: Settings) -> None:
    base = settings.elasticsearch_url.rstrip("/")
    index = settings.elasticsearch_index
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.put(
            f"{base}/{index}",
            json={
                "mappings": {
                    "properties": {
                        "source_id": {
                            "type": "text",
                            "fields": {"keyword": {"type": "keyword"}},
                        },
                        "vector": {
                            "type": "dense_vector",
                            "dims": settings.embedding_dimensions,
                            "index": False,
                        },
                    }
                }
            },
        )
        if response.status_code not in {200, 400}:
            response.raise_for_status()


async def replace_chunk_index(
    settings: Settings,
    source_type: Literal["ATTACHMENT", "KNOWLEDGE"],
    source_id: str,
    digest: str,
    chunks: list[DocumentChunk],
    vectors: list[list[float]],
) -> None:
    base = settings.elasticsearch_url.rstrip("/")
    index = settings.elasticsearch_index
    async with httpx.AsyncClient(timeout=10) as client:
        deleted = await client.post(
            f"{base}/{index}/_delete_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={"query": {"term": {"source_id": source_id}}},
        )
        deleted.raise_for_status()
        for number, (chunk, vector) in enumerate(zip(chunks, vectors), start=1):
            chunk_id = f"{source_id}:{number}"
            indexed = await client.put(
                f"{base}/{index}/_doc/{quote(chunk_id, safe='')}",
                params={"refresh": "true"},
                json={
                    "source_type": source_type,
                    "source_id": source_id,
                    "content_sha256": digest,
                    "chunk_id": chunk_id,
                    "page_numbers": chunk.pages,
                    "text": chunk.text,
                    "document_title": chunk.document_title,
                    "headings": chunk.headings,
                    "vector": vector,
                },
            )
            indexed.raise_for_status()
