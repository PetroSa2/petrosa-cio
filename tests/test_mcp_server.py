"""Tests for strategist MCP server behavior."""

import pytest

from apps.strategist.mcp_server import MCPServer
from core.config_manager import ConfigManager


class FakeCollection:
    def __init__(self):
        self.documents = []

    async def insert_one(self, document):
        self.documents.append(document)


class FakeRedis:
    def __init__(self):
        self.deleted_keys = []

    async def delete(self, key: str):
        self.deleted_keys.append(key)


class FakeRoiEngine:
    async def get_earnings_summary(self, window_hours: int = 168):
        return {
            "window_hours": window_hours,
            "actual_pnl": 10.0,
            "shadow_roi": 5.0,
            "governance_status": "ACTIVE",
        }


@pytest.mark.asyncio
async def test_mcp_server_lists_tools_from_defaults_models():
    server = MCPServer(roi_engine=FakeRoiEngine())

    response = await server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )

    assert "result" in response
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "get_RiskLimits" in names
    assert "set_RiskLimits" in names
    assert "rollback_to_version" in names
    assert "get_earnings_summary" in names


@pytest.mark.asyncio
async def test_get_earnings_summary_tool_returns_roi_snapshot():
    server = MCPServer(roi_engine=FakeRoiEngine())

    response = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "get_earnings_summary",
                "arguments": {"window_hours": 72},
            },
        }
    )

    assert "result" in response
    assert response["result"]["window_hours"] == 72
    assert response["result"]["actual_pnl"] == pytest.approx(10.0)
    assert response["result"]["shadow_roi"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_set_tool_rejects_short_thought_trace():
    server = MCPServer()

    response = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "set_RiskLimits",
                "arguments": {
                    "payload": {
                        "max_drawdown_pct": 0.2,
                        "max_position_size_pct": 0.1,
                        "volatility_scale_threshold": 0.03,
                    },
                    "thought_trace": "too short",
                },
            },
        }
    )

    assert "error" in response
    assert "thought_trace" in response["error"]["message"]


@pytest.mark.asyncio
async def test_set_tool_persists_thought_trace_to_audit_collection():
    collection = FakeCollection()
    config_manager = ConfigManager(audit_collection=collection)
    server = MCPServer(config_manager=config_manager)

    trace = (
        "The update reduces risk concentration by lowering drawdown and position limits "
        "while preserving deterministic execution behavior under current volatility."
    )

    response = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "set_RiskLimits",
                "arguments": {
                    "payload": {
                        "max_drawdown_pct": 0.15,
                        "max_position_size_pct": 0.08,
                        "volatility_scale_threshold": 0.025,
                    },
                    "thought_trace": trace,
                },
            },
        }
    )

    assert "result" in response
    assert response["result"]["updated"] is True
    assert len(collection.documents) == 1
    assert collection.documents[0]["thought_trace"] == trace
    assert collection.documents[0]["model"] == "RiskLimits"


@pytest.mark.asyncio
async def test_rollback_tool_restores_previous_version_and_logs_event():
    audit_collection = FakeCollection()
    history_collection = FakeCollection()
    redis = FakeRedis()
    config_manager = ConfigManager(
        audit_collection=audit_collection,
        history_collection=history_collection,
        redis_client=redis,
    )
    server = MCPServer(config_manager=config_manager)

    trace = "a" * 120
    first = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "set_RiskLimits",
                "arguments": {
                    "payload": {
                        "max_drawdown_pct": 0.2,
                        "max_position_size_pct": 0.1,
                        "volatility_scale_threshold": 0.03,
                    },
                    "thought_trace": trace,
                },
            },
        }
    )
    _ = first
    second = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "set_RiskLimits",
                "arguments": {
                    "payload": {
                        "max_drawdown_pct": 0.12,
                        "max_position_size_pct": 0.07,
                        "volatility_scale_threshold": 0.02,
                    },
                    "thought_trace": trace,
                },
            },
        }
    )

    # Snapshot should be created before applying the second patch.
    assert len(history_collection.documents) == 1

    target_audit_id = second["result"]["audit"]["audit_id"]

    rollback = await server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "rollback_to_version",
                "arguments": {
                    "audit_id": target_audit_id,
                    "reason": "restore known safe profile",
                },
            },
        }
    )

    assert rollback["result"]["rolled_back"] is True
    assert rollback["result"]["event"]["event_type"] == "config_rollback"
    assert (
        rollback["result"]["event"]["rollback_reason"] == "restore known safe profile"
    )
    assert history_collection.documents != []
    assert redis.deleted_keys == ["policy:RiskLimits"]
