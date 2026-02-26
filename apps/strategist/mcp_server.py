"""MCP-compatible strategist server with dynamic schema tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from core.config_manager import ConfigManager
from core.utils.schema_parser import generate_tools


def validate_thought_trace(
    fn: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    @wraps(fn)
    async def wrapper(self: MCPServer, *args: Any, **kwargs: Any) -> dict[str, Any]:
        arguments = kwargs.get("arguments") or {}
        thought_trace = str(arguments.get("thought_trace", ""))
        if len(thought_trace) < 100:
            raise ValueError("thought_trace must be at least 100 characters")
        return await fn(self, *args, **kwargs)

    return wrapper


class MCPServer:
    """Minimal MCP JSON-RPC server supporting stdio transport."""

    def __init__(
        self,
        *,
        module_path: str = "apps.strategist.defaults",
        config_manager: ConfigManager | None = None,
    ):
        self.module_path = module_path
        self.config_manager = config_manager or ConfigManager()
        self.tools = generate_tools(module_path)
        self.tools_by_name = {tool["name"]: tool for tool in self.tools}

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method")
        params = request.get("params") or {}
        request_id = request.get("id")

        try:
            if method == "initialize":
                result = {"protocolVersion": "2026-02", "server": "petrosa-cio-mcp"}
            elif method == "tools/list":
                result = {"tools": self.tools}
            elif method == "tools/call":
                result = await self._call_tool(params)
            else:
                raise ValueError(f"unsupported method: {method}")

            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}

        if tool_name not in self.tools_by_name:
            raise ValueError(f"unknown tool: {tool_name}")

        if tool_name.startswith("get_"):
            return await self._handle_get(tool_name)

        if tool_name.startswith("set_"):
            return await self._handle_set(tool_name, arguments=arguments)

        raise ValueError(f"unsupported tool: {tool_name}")

    async def _handle_get(self, tool_name: str) -> dict[str, Any]:
        model_name = tool_name.removeprefix("get_")
        return {
            "model": model_name,
            "config": self.config_manager.get_config(model_name),
        }

    @validate_thought_trace
    async def _handle_set(
        self,
        tool_name: str,
        *,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        model_name = tool_name.removeprefix("set_")
        payload = arguments.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")

        audit = await self.config_manager.set_config(
            model_name,
            payload,
            thought_trace=arguments["thought_trace"],
        )
        return {"model": model_name, "updated": True, "audit": audit}

    async def run_stdio(self) -> None:
        """Run JSON-RPC loop over stdin/stdout (one JSON request per line)."""
        while True:
            line = await asyncio.to_thread(input)
            if not line:
                continue

            request = json.loads(line)
            response = await self.handle_request(request)
            print(json.dumps(response), flush=True)


def create_server(module_path: str = "apps.strategist.defaults") -> MCPServer:
    return MCPServer(module_path=module_path)
