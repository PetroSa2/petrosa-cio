import logging
import os
from datetime import datetime
from typing import Any, Protocol

try:
    from qdrant_client import AsyncQdrantClient, models

    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning(
        "qdrant-client not installed. QdrantVectorClient will be unavailable."
    )

logger = logging.getLogger(__name__)


class VectorClientProtocol(Protocol):
    """
    Interface for Vector Database clients.
    Ensures pluggable backends (Qdrant, Pinecone, etc.).
    """

    async def query(self, strategy_id: str, limit: int = 5) -> str:
        """Queries historical context for a specific strategy."""
        ...

    async def upsert(self, strategy_id: str, payload: dict[str, Any]) -> bool:
        """Stores a new reasoning event or decision in the vector store."""
        ...


class QdrantVectorClient:
    """Production client for Qdrant Vector DB."""

    def __init__(self):
        if not QDRANT_AVAILABLE:
            raise ImportError("qdrant-client is required for QdrantVectorClient")

        url = os.getenv("QDRANT_URL")
        api_key = os.getenv("QDRANT_API_KEY")
        self.client = AsyncQdrantClient(url=url, api_key=api_key)
        self.collection_name = "cio_strategy_history"

    async def query(self, strategy_id: str, limit: int = 5) -> str:
        if not QDRANT_AVAILABLE:
            return ""
        try:
            dummy_vector = [0.0] * 1536
            results = await self.client.search(
                collection_name=self.collection_name,
                query_vector=dummy_vector,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="strategy_id",
                            match=models.MatchValue(value=strategy_id),
                        )
                    ]
                ),
                limit=limit,
                with_payload=True,
            )
            if not results:
                return ""
            return "\n".join(
                [
                    f"[{res.payload.get('timestamp')}] {res.payload.get('event_type')}: {res.payload.get('summary') or res.payload.get('thought_trace')}"
                    for res in results
                ]
            )
        except Exception as e:
            logger.warning(f"Qdrant query failed: {e}")
            return ""

    async def upsert(self, strategy_id: str, payload: dict[str, Any]) -> bool:
        """Stores decision audit trails in Qdrant."""
        if not QDRANT_AVAILABLE:
            return False
        try:
            import uuid

            # Ensure timestamp is present for the T-Junction audit path
            if "timestamp" not in payload:
                payload["timestamp"] = datetime.utcnow().isoformat()
            payload["strategy_id"] = strategy_id

            await self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector=[0.0] * 1536,  # Placeholder for future embedding
                        payload=payload,
                    )
                ],
            )
            return True
        except Exception as e:
            logger.error(f"Qdrant upsert failed: {e}")
            return False


class MockVectorClient:
    """Mock for local development."""

    def __init__(self):
        self._storage = []

    async def query(self, strategy_id: str, limit: int = 5) -> str:
        return "Mock Historical Context: Strategy has been stable."

    async def upsert(self, strategy_id: str, payload: dict[str, Any]) -> bool:
        self._storage.append({"strategy_id": strategy_id, **payload})
        logger.debug(f"Mock Vector Upsert: {strategy_id}")
        return True
