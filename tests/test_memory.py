"""Tests for institutional memory vector indexing and retrieval."""

import pytest

from apps.strategist.memory import InstitutionalMemoryService
from core.config_manager import ConfigManager
from core.db.vector_client import VectorMemoryClient


@pytest.mark.asyncio
async def test_vector_client_deduplicates_semantically_identical_trace():
    client = VectorMemoryClient()
    metadata = {"audit_id": "a1"}

    first = await client.upsert_trace(trace="same thought trace", metadata=metadata)
    second = await client.upsert_trace(trace="same thought trace", metadata=metadata)

    assert first["indexed"] is True
    assert second["indexed"] is False
    assert second["deduplicated"] is True


@pytest.mark.asyncio
async def test_memory_service_search_returns_metadata():
    service = InstitutionalMemoryService(vector_client=VectorMemoryClient())
    await service.index_audit_event(
        {
            "audit_id": "audit-1",
            "event_type": "config_update",
            "model": "RiskLimits",
            "updated_at": "2026-02-26T00:00:00+00:00",
            "thought_trace": (
                "Previous drawdown breach happened after aggressive leverage increase."
            ),
            "payload": {"potential_pnl": -12.5},
        }
    )

    result = await service.search_knowledge_base(
        query="drawdown leverage breach", top_k=3
    )

    assert result["results_count"] >= 1
    assert result["results"][0]["metadata"]["model"] == "RiskLimits"
    assert "timestamp" in result["results"][0]["metadata"]


class FakeMemoryService:
    def __init__(self):
        self.events = []

    async def index_audit_event(self, document):
        self.events.append(document)
        return {"indexed": True, "deduplicated": False}


@pytest.mark.asyncio
async def test_config_manager_auto_indexes_thought_trace_updates():
    memory_service = FakeMemoryService()
    manager = ConfigManager(memory_service=memory_service)

    await manager.set_config(
        "RiskLimits",
        {
            "max_drawdown_pct": 0.1,
            "max_position_size_pct": 0.05,
            "volatility_scale_threshold": 0.02,
        },
        thought_trace=(
            "Use tighter drawdown and position limits due to repeated volatility spikes."
        ),
    )

    assert len(memory_service.events) == 1
    assert memory_service.events[0]["event_type"] == "config_update"
    assert "thought_trace" in memory_service.events[0]
