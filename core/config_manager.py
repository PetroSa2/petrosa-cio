"""Configuration manager for strategist MCP tool operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class ConfigManager:
    """In-memory config manager with optional MongoDB audit persistence."""

    def __init__(self, audit_collection: Any | None = None):
        self.audit_collection = audit_collection
        self._configs: dict[str, dict[str, Any]] = {}

    def get_config(self, model_name: str) -> dict[str, Any] | None:
        return self._configs.get(model_name)

    async def set_config(
        self,
        model_name: str,
        payload: dict[str, Any],
        *,
        thought_trace: str,
        actor: str = "mcp_strategist",
    ) -> dict[str, Any]:
        document = {
            "model": model_name,
            "payload": payload,
            "thought_trace": thought_trace,
            "actor": actor,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self._configs[model_name] = payload

        if self.audit_collection is not None:
            await self.audit_collection.insert_one(document)

        return document
