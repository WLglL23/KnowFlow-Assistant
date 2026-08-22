from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


class RetrievalError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Evidence:
    source_type: str
    source_id: str
    chunk_id: str
    filename: str
    pages: tuple[int, ...]
    text: str


class _EmbeddingItem(BaseModel):
    embedding: list[float]


class _EmbeddingResponse(BaseModel):
    data: list[_EmbeddingItem]


class _RerankItem(BaseModel):
    index: int


class _RerankResponse(BaseModel):
    results: list[_RerankItem] = Field(default_factory=lambda: list[_RerankItem]())


class _SearchSource(BaseModel):
    source_type: str = "ATTACHMENT"
    source_id: str
    chunk_id: str
    page_numbers: list[int]
    text: str


class _SearchHit(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    document_id: str = Field(alias="_id")
    source: _SearchSource = Field(alias="_source")


class _SearchHits(BaseModel):
    hits: list[_SearchHit]


class _SearchResponse(BaseModel):
    hits: _SearchHits


class EvidenceTokenizerV1:
    version = "evidence-tokenizer-v1"
    _pattern = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    @classmethod
    def count(cls, text: str) -> int:
        return len(cls._pattern.findall(text))


class AttachmentRetrieval:
    def __init__(
        self,
        *,
        elasticsearch_url: str,
        elasticsearch_index: str,
        embedding_base_url: str | None,
        embedding_api_key: SecretStr | None,
        embedding_model: str | None,
        embedding_dimensions: int,
        rerank_base_url: str | None,
        rerank_api_key: SecretStr | None,
        rerank_model: str | None,
    ) -> None:
        self._search_url = elasticsearch_url.rstrip("/")
        self._index = elasticsearch_index
        self._embedding_base_url = embedding_base_url
        self._embedding_api_key = embedding_api_key
        self._embedding_model = embedding_model
        self._embedding_dimensions = embedding_dimensions
        self._rerank_base_url = rerank_base_url
        self._rerank_api_key = rerank_api_key
        self._rerank_model = rerank_model

    def ensure_configured(self) -> None:
        if not all(
            (
                self._embedding_base_url,
                self._embedding_api_key,
                self._embedding_model,
                self._rerank_base_url,
                self._rerank_api_key,
                self._rerank_model,
            )
        ):
            raise RetrievalError("retrieval models are not configured")

    async def embed(self, query: str, *, timeout_seconds: float) -> list[float]:
        vectors = await self.embed_many([query], timeout_seconds=timeout_seconds)
        return vectors[0]

    async def embed_many(
        self, queries: list[str], *, timeout_seconds: float
    ) -> list[list[float]]:
        self.ensure_configured()
        if not queries:
            return []
        assert self._embedding_base_url and self._embedding_api_key
        try:
            async with httpx.AsyncClient(timeout=max(timeout_seconds, 0.001)) as client:
                response = await client.post(
                    f"{self._embedding_base_url.rstrip('/')}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._embedding_api_key.get_secret_value()}"
                    },
                    json={"model": self._embedding_model, "input": queries},
                )
                response.raise_for_status()
                payload = _EmbeddingResponse.model_validate(response.json())
            vectors = [item.embedding for item in payload.data]
            if len(vectors) != len(queries) or any(
                len(vector) != self._embedding_dimensions for vector in vectors
            ):
                raise RetrievalError("embedding dimension mismatch")
            return vectors
        except RetrievalError:
            raise
        except httpx.HTTPStatusError as exc:
            raise RetrievalError(
                "embedding failed", retryable=exc.response.status_code >= 500
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetrievalError("embedding failed", retryable=True) from exc
        except (IndexError, TypeError, ValueError, ValidationError) as exc:
            raise RetrievalError("embedding response invalid", retryable=True) from exc

    async def hybrid(
        self,
        query: str,
        vector: list[float],
        source_filenames: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> list[Evidence]:
        if not source_filenames:
            return []
        source_filter = {"terms": {"source_id.keyword": list(source_filenames)}}
        try:
            group_rankings: list[list[_SearchHit]] = []
            async with httpx.AsyncClient(timeout=max(timeout_seconds, 0.001)) as client:
                for source_type in ("ATTACHMENT", "KNOWLEDGE"):
                    filters = [
                        source_filter,
                        {"term": {"source_type.keyword": source_type}},
                    ]
                    bm25 = {
                        "size": 40,
                        "query": {
                            "bool": {
                                "must": [{"match": {"text": query}}],
                                "filter": filters,
                            }
                        },
                    }
                    dense = {
                        "size": 40,
                        "query": {
                            "script_score": {
                                "query": {"bool": {"filter": filters}},
                                "script": {
                                    "source": (
                                        "cosineSimilarity(params.query_vector, 'vector') + 1.0"
                                    ),
                                    "params": {"query_vector": vector},
                                },
                            }
                        },
                    }
                    bm25_response = await client.post(
                        f"{self._search_url}/{self._index}/_search", json=bm25
                    )
                    bm25_response.raise_for_status()
                    dense_response = await client.post(
                        f"{self._search_url}/{self._index}/_search", json=dense
                    )
                    dense_response.raise_for_status()
                    bm25_hits = _SearchResponse.model_validate(
                        bm25_response.json()
                    ).hits.hits
                    dense_hits = _SearchResponse.model_validate(
                        dense_response.json()
                    ).hits.hits
                    group_rankings.append(self._rrf(bm25_hits, dense_hits))
            ranked = self._rrf(*group_rankings)
            return [
                self._to_evidence(hit, source_filenames)
                for hit in ranked
                if self._valid_hit(hit, source_filenames)
            ]
        except httpx.HTTPError as exc:
            raise RetrievalError("search failed") from exc
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RetrievalError("search response invalid") from exc

    async def rerank(
        self, query: str, evidence: list[Evidence], *, timeout_seconds: float
    ) -> list[Evidence]:
        self.ensure_configured()
        if not evidence:
            return []
        assert self._rerank_base_url and self._rerank_api_key
        try:
            async with httpx.AsyncClient(timeout=max(timeout_seconds, 0.001)) as client:
                response = await client.post(
                    f"{self._rerank_base_url.rstrip('/')}/reranks",
                    headers={"Authorization": f"Bearer {self._rerank_api_key.get_secret_value()}"},
                    json={
                        "model": self._rerank_model,
                        "query": query,
                        "documents": [item.text for item in evidence],
                    },
                )
                response.raise_for_status()
                results = _RerankResponse.model_validate(response.json()).results
            if not results:
                return evidence
            indexes = [
                item.index
                for item in results
                if 0 <= item.index < len(evidence)
            ]
            return [evidence[index] for index in indexes] + [
                item for index, item in enumerate(evidence) if index not in indexes
            ]
        except httpx.HTTPStatusError as exc:
            raise RetrievalError(
                "rerank failed", retryable=exc.response.status_code >= 500
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetrievalError("rerank failed", retryable=True) from exc
        except (TypeError, ValueError, ValidationError) as exc:
            raise RetrievalError("rerank response invalid", retryable=True) from exc

    @staticmethod
    def select(
        evidence: list[Evidence], *, max_chunks: int, max_tokens: int
    ) -> tuple[list[Evidence], int]:
        selected: list[Evidence] = []
        token_count = 0
        for item in evidence:
            count = EvidenceTokenizerV1.count(item.text)
            if count > max_tokens - token_count:
                continue
            selected.append(item)
            token_count += count
            if len(selected) == max_chunks:
                break
        return selected, token_count

    @staticmethod
    def token_count(evidence: Evidence) -> int:
        return EvidenceTokenizerV1.count(evidence.text)

    @staticmethod
    def _rrf(*rankings: list[_SearchHit]) -> list[_SearchHit]:
        scores: dict[str, float] = {}
        hits: dict[str, _SearchHit] = {}
        for ranking in rankings:
            for rank, hit in enumerate(ranking, 1):
                key = hit.document_id
                if key:
                    scores[key] = scores.get(key, 0.0) + 1 / (60 + rank)
                    hits[key] = hit
        return [hits[key] for key in sorted(scores, key=lambda key: scores[key], reverse=True)]

    @staticmethod
    def _valid_hit(hit: _SearchHit, filenames: dict[str, str]) -> bool:
        return hit.source.source_id in filenames

    @staticmethod
    def _to_evidence(hit: _SearchHit, filenames: dict[str, str]) -> Evidence:
        source = hit.source
        return Evidence(
            source_type=source.source_type,
            source_id=source.source_id,
            chunk_id=source.chunk_id,
            filename=filenames[source.source_id],
            pages=tuple(source.page_numbers),
            text=source.text,
        )
