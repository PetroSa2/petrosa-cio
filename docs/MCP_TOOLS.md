# MCP Tools

This document describes MCP tools exposed by `apps/strategist/mcp_server.py`.

## Transport
- JSON-RPC via STDIO (`MCPServer.run_stdio`) for local desktop integration.

## Discovery
- Tool definitions are generated dynamically from Pydantic models in `apps/strategist/defaults.py`.
- Every model produces:
  - `get_<ModelName>`
  - `set_<ModelName>`

## Write Guard (`thought_trace`)
All write tools (`set_*`) require:
- `thought_trace` string
- Minimum length: `100` characters

If missing or shorter than 100 chars, the call is rejected.

## Audit Link
Every successful `set_*` call persists an audit document through `ConfigManager` including:
- `model`
- `payload`
- `thought_trace`
- `actor`
- `updated_at`

When configured with Mongo collection, this audit document is stored for retrospective reviews.

## Example Call
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "set_RiskLimits",
    "arguments": {
      "payload": {
        "max_drawdown_pct": 0.15,
        "max_position_size_pct": 0.08,
        "volatility_scale_threshold": 0.025
      },
      "thought_trace": "...at least 100 characters of reasoning explaining why this configuration update is safe..."
    }
  }
}
```
