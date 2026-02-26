"""Vector memory client with async embedding, deduplication, and retrieval."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from typing import Any


class VectorMemoryClient:
    """Small async vector store abstraction for institutional memory."""

    def __init__(
        self,
        collection: Any | None = None,
        *,
        model_name: str = "text-embedding-3-small",
        dedupe_threshold: float = 0.97,
    ):
        self.collection = collection
        self.model_name = model_name
        self.dedupe_threshold = dedupe_threshold
        self._documents: list[dict[str, Any]] = []

    async def upsert_trace(
        self,
        *,
        trace: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        embedding = await self.embed_text(trace)
        duplicate = await self._find_duplicate(embedding)
        if duplicate is not None:
            return {
                "indexed": False,
                "deduplicated": True,
                "reason": "semantic_duplicate",
                "existing_id": duplicate.get("id"),
            }

        document = {
            "id": self._doc_id(trace=trace, metadata=metadata),
            "trace": trace,
            "embedding": embedding,
            "metadata": metadata,
            "embedding_model": self.model_name,
            "indexed_at": datetime.now(UTC).isoformat(),
        }
        self._documents.append(document)

        if self.collection is not None:
            await self.collection.insert_one(document)

        return {"indexed": True, "deduplicated": False, "id": document["id"]}

    async def search(
        self,
        *,
        query: str,
        top_k: int = 5,
        min_similarity: float = 0.4,
    ) -> list[dict[str, Any]]:
        query_embedding = await self.embed_text(query)
        candidates = await self._all_documents()
        scored: list[tuple[float, dict[str, Any]]] = []
        for doc in candidates:
            score = self.cosine_similarity(query_embedding, doc.get("embedding", []))
            if score >= min_similarity:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "id": doc.get("id"),
                "thought_trace": doc.get("trace"),
                "similarity": round(score, 6),
                "metadata": doc.get("metadata", {}),
            }
            for score, doc in scored[:top_k]
        ]

    async def embed_text(self, text: str) -> list[float]:
        # Deterministic local embedding fallback for offline/test environments.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        embedding = []
        for idx in range(0, 32, 4):
            chunk = digest[idx : idx + 4]
            val = int.from_bytes(chunk, byteorder="big", signed=False)
            embedding.append((val % 10_000) / 10_000.0)
        return embedding

    async def _find_duplicate(self, embedding: list[float]) -> dict[str, Any] | None:
        for doc in await self._all_documents():
            score = self.cosine_similarity(embedding, doc.get("embedding", []))
            if score >= self.dedupe_threshold:
                return doc
        return None

    async def _all_documents(self) -> list[dict[str, Any]]:
        if self.collection is None:
            return list(self._documents)
        if hasattr(self.collection, "find"):
            cursor = self.collection.find({})
            if hasattr(cursor, "to_list"):
                return await cursor.to_list(length=10_000)
            return [item async for item in cursor]
        return list(self._documents)

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _doc_id(*, trace: str, metadata: dict[str, Any]) -> str:
        payload = f"{trace}|{metadata.get('audit_id','')}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
