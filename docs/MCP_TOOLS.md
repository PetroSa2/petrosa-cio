# Retired MCP Tools

This is a historical record of a deleted integration. No MCP server is shipped by `petrosa-cio`,
and `apps/strategist/mcp_server.py` is not an available runtime path. Do not use this document to
configure or operate the service. The current strategy-analysis decision is documented in
`llm-strategist/architecture/overview.md`.

The MCP surface was not rebuilt because it provided configuration tooling rather than missing
decision value. The strategy-specific LLM analysis stage is scheduled for separate removal after
its runtime audit fields are replaced with deterministic values or explicit unavailable markers.
