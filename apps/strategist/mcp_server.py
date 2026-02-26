"""MCP-compatible strategist server with dynamic schema tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from apps.nurse.roi_engine import ShadowROIEngine
from apps.strategist.memory import InstitutionalMemoryService
from core.config_manager import ConfigManager
from core.utils.schema_parser import discover_schema_models, generate_tools


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
        roi_engine: ShadowROIEngine | None = None,
        memory_service: InstitutionalMemoryService | None = None,
    ):
        self.module_path = module_path
        self.models = discover_schema_models(module_path)
        self.config_manager = config_manager or ConfigManager()
        self.roi_engine = roi_engine or ShadowROIEngine()
        self.memory_service = memory_service or InstitutionalMemoryService()
        self.config_manager.set_payload_validator(self._validate_model_payload)
        self.tools = generate_tools(module_path)
        self.tools.append(
            {
                "name": "rollback_to_version",
                "description": "Rollback configuration state to a previous audit version.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "audit_id": {"type": "string"},
                        "reason": {"type": "string", "minLength": 3},
                    },
                    "required": ["audit_id", "reason"],
                },
                "mode": "write",
            }
        )
        self.tools.append(
            {
                "name": "get_earnings_summary",
                "description": "Return governance summary with Actual PnL and Shadow ROI.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "window_hours": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 2160,
                        }
                    },
                },
                "mode": "read",
            }
        )
        self.tools.append(
            {
                "name": "search_knowledge_base",
                "description": "Semantic retrieval over indexed reasoning traces.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 3},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query"],
                },
                "mode": "read",
            }
        )
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
            if tool_name == "get_earnings_summary":
                return await self._handle_earnings_summary(arguments=arguments)
            return await self._handle_get(tool_name)
        if tool_name == "search_knowledge_base":
            return await self._handle_search_knowledge_base(arguments=arguments)

        if tool_name.startswith("set_"):
            return await self._handle_set(tool_name, arguments=arguments)
        if tool_name == "rollback_to_version":
            return await self._handle_rollback(arguments=arguments)

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

    async def _handle_rollback(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        audit_id = str(arguments.get("audit_id", ""))
        reason = str(arguments.get("reason", ""))
        if not audit_id:
            raise ValueError("audit_id is required")
        if len(reason) < 3:
            raise ValueError("reason must be at least 3 characters")

        event = await self.config_manager.rollback_to_version(audit_id, reason=reason)
        return {"rolled_back": True, "event": event}

    async def _handle_earnings_summary(
        self,
        *,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        window_hours = int(arguments.get("window_hours", 24 * 7))
        if window_hours <= 0:
            raise ValueError("window_hours must be a positive integer")
        return await self.roi_engine.get_earnings_summary(window_hours=window_hours)

    async def _handle_search_knowledge_base(
        self,
        *,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        top_k = int(arguments.get("top_k", 5))
        return await self.memory_service.search_knowledge_base(query=query, top_k=top_k)

    def _validate_model_payload(
        self,
        model_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        model = self.models.get(model_name)
        if model is None:
            raise ValueError(f"unknown model for validation: {model_name}")
        validated = model(**payload)
        return validated.model_dump()

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
