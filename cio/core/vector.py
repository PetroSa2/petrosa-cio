import logging
from typing import Any, Protocol

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


class MockVectorClient:
    """Mock for local development."""

    def __init__(self, llm_client=None):
        self._storage = []
        self.llm_client = llm_client

    async def query(self, strategy_id: str, limit: int = 5) -> str:
        return "Mock Historical Context: Strategy has been stable."

    async def upsert(self, strategy_id: str, payload: dict[str, Any]) -> bool:
        self._storage.append({"strategy_id": strategy_id, **payload})
        logger.debug(f"Mock Vector Upsert: {strategy_id}")
        return True
