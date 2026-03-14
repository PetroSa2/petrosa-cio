import pytest
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Mock the entire qdrant_client module before importing from cio.core.vector
mock_qdrant = MagicMock()
sys.modules["qdrant_client"] = mock_qdrant
sys.modules["qdrant_client.models"] = MagicMock()

from cio.core.vector import QdrantVectorClient

@pytest.mark.asyncio
async def test_qdrant_upsert_with_embeddings():
    # Mock LLM Client
    mock_llm = AsyncMock()
    mock_llm.embed.return_value = [0.1] * 1536
    
    # Patch the AsyncQdrantClient within the vector module
    with patch("cio.core.vector.AsyncQdrantClient") as mock_qdrant_cls:
        mock_qdrant_instance = mock_qdrant_cls.return_value
        mock_qdrant_instance.upsert = AsyncMock(return_value=True)
        
        client = QdrantVectorClient(llm_client=mock_llm)
        
        payload = {
            "thought_trace": "Executing trade due to bull regime",
            "event_type": "decision"
        }
        
        success = await client.upsert("strategy_1", payload)
        
        assert success is True
        mock_llm.embed.assert_called_once_with("Executing trade due to bull regime")
        
        # Verify qdrant.upsert was called
        mock_qdrant_instance.upsert.assert_called_once()

@pytest.mark.asyncio
async def test_qdrant_query_with_embeddings():
    mock_llm = AsyncMock()
    mock_llm.embed.return_value = [0.2] * 1536
    
    with patch("cio.core.vector.AsyncQdrantClient") as mock_qdrant_cls:
        mock_qdrant_instance = mock_qdrant_cls.return_value
        mock_qdrant_instance.search = AsyncMock(return_value=[])
        
        client = QdrantVectorClient(llm_client=mock_llm)
        await client.query("strategy_1")
        
        # Verify llm_client.embed was called for the query
        mock_llm.embed.assert_called_once()
        # Verify qdrant.search was called
        mock_qdrant_instance.search.assert_called_once()
