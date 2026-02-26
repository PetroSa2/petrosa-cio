"""Tests for dynamic MCP schema tool generation."""

from core.utils.schema_parser import discover_schema_models, generate_tools


def test_discover_schema_models_from_defaults_module():
    models = discover_schema_models("apps.strategist.defaults")

    assert "RiskLimits" in models
    assert "ExecutionPolicy" in models


def test_generate_tools_builds_read_and_write_tool_pairs():
    tools = generate_tools("apps.strategist.defaults")
    names = {tool["name"] for tool in tools}

    assert "get_RiskLimits" in names
    assert "set_RiskLimits" in names
    assert "get_ExecutionPolicy" in names
    assert "set_ExecutionPolicy" in names

    set_risk_limits = next(tool for tool in tools if tool["name"] == "set_RiskLimits")
    assert "thought_trace" in set_risk_limits["input_schema"]["properties"]
    assert "payload" in set_risk_limits["input_schema"]["required"]
    assert "thought_trace" in set_risk_limits["input_schema"]["required"]
