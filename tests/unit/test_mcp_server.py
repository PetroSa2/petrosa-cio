from unittest.mock import MagicMock, patch

import pytest

from cio.mcp_server import MCPServer


@pytest.mark.asyncio
async def test_mcp_server_rate_governor_throttling():
    """Test that MCPServer throttles write operations when RateGovernor is throttled."""
    # Mock dependencies
    mock_config_manager = MagicMock()
    mock_roi_engine = MagicMock()
    mock_memory_service = MagicMock()

    with (
        patch("cio.mcp_server.discover_schema_models", return_value={}),
        patch(
            "cio.mcp_server.generate_tools",
            return_value=[{"name": "set_test", "mode": "write"}],
        ),
        patch("cio.mcp_server.RateGovernor") as MockRateGovernor,
    ):
        mock_rate_governor = MockRateGovernor.return_value
        mock_rate_governor.is_throttled.return_value = True
        mock_rate_governor.get_status.return_value = {"usage_pct": 95}

        server = MCPServer(
            config_manager=mock_config_manager,
            roi_engine=mock_roi_engine,
            memory_service=mock_memory_service,
        )

        # Test throttling for 'set_' tool
        params = {
            "name": "set_test",
            "arguments": {"payload": {}, "thought_trace": "x" * 101},
        }
        result = await server._call_tool(params)

        assert result["isError"] is True
        assert "BACK OFF" in result["content"][0]["text"]
        assert "95%" in result["content"][0]["text"]

        # Test throttling for 'rollback_to_version'
        params = {
            "name": "rollback_to_version",
            "arguments": {"audit_id": "1", "reason": "test"},
        }
        result = await server._call_tool(params)

        assert result["isError"] is True
        assert "BACK OFF" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_mcp_server_no_throttling_for_reads():
    """Test that MCPServer does NOT throttle read operations even when RateGovernor is throttled."""
    # Mock dependencies
    mock_config_manager = MagicMock()
    mock_config_manager.get_config.return_value = {}
    mock_roi_engine = MagicMock()
    mock_memory_service = MagicMock()

    with (
        patch("cio.mcp_server.discover_schema_models", return_value={}),
        patch(
            "cio.mcp_server.generate_tools",
            return_value=[{"name": "get_test", "mode": "read"}],
        ),
        patch("cio.mcp_server.RateGovernor") as MockRateGovernor,
    ):
        mock_rate_governor = MockRateGovernor.return_value
        mock_rate_governor.is_throttled.return_value = True

        server = MCPServer(
            config_manager=mock_config_manager,
            roi_engine=mock_roi_engine,
            memory_service=mock_memory_service,
        )

        # Test NO throttling for 'get_' tool
        params = {"name": "get_test", "arguments": {}}
        result = await server._call_tool(params)

        assert "isError" not in result
        assert result["model"] == "test"
