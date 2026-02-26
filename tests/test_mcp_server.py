"""Tests for strategist MCP server behavior."""

import pytest

from apps.strategist.mcp_server import MCPServer
from core.config_manager import ConfigManager


class FakeCollection:
    def __init__(self):
        self.documents = []

    async def insert_one(self, document):
        self.documents.append(document)


@pytest.mark.asyncio
async def test_mcp_server_lists_tools_from_defaults_models():
    server = MCPServer()

    response = await server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )

    assert "result" in response
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "get_RiskLimits" in names
    assert "set_RiskLimits" in names


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
