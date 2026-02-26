"""Configuration manager with snapshot and rollback support."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

from apps.nurse.enforcer import ConfigSnapshotter
from apps.strategist.memory import InstitutionalMemoryService

PayloadValidator = Callable[[str, dict[str, Any]], dict[str, Any]]


class ConfigManager:
    """Config manager with optional MongoDB/Redis side effects."""

    def __init__(
        self,
        audit_collection: Any | None = None,
        history_collection: Any | None = None,
        redis_client: Any | None = None,
        payload_validator: PayloadValidator | None = None,
        memory_service: InstitutionalMemoryService | None = None,
    ):
        self.audit_collection = audit_collection
        self.snapshotter = ConfigSnapshotter(history_collection=history_collection)
        self.redis_client = redis_client
        self.payload_validator = payload_validator
        self.memory_service = memory_service
        self._configs: dict[str, dict[str, Any]] = {}
        self._audit_events: list[dict[str, Any]] = []

    def set_payload_validator(self, validator: PayloadValidator) -> None:
        self.payload_validator = validator

    def get_config(self, model_name: str) -> dict[str, Any] | None:
        return self._configs.get(model_name)

    async def _persist_audit(self, document: dict[str, Any]) -> None:
        self._audit_events.append(document)
        if self.audit_collection is not None:
            await self.audit_collection.insert_one(document)
        if self.memory_service is not None:
            await self.memory_service.index_audit_event(document)

    def _validate(self, model_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.payload_validator is None:
            return payload
        return self.payload_validator(model_name, payload)

    async def set_config(
        self,
        model_name: str,
        payload: dict[str, Any],
        *,
        thought_trace: str,
        actor: str = "mcp_strategist",
    ) -> dict[str, Any]:
        validated_payload = self._validate(model_name, payload)
        previous = self._configs.get(model_name)

        audit_id = str(uuid4())
        if previous is not None:
            await self.snapshotter.snapshot(
                model_name=model_name,
                payload=previous,
                source_audit_id=audit_id,
            )

        self._configs[model_name] = validated_payload

        document = {
            "audit_id": audit_id,
            "event_type": "config_update",
            "model": model_name,
            "payload": validated_payload,
            "thought_trace": thought_trace,
            "actor": actor,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        await self._persist_audit(document)

        return document

    async def rollback_to_version(
        self,
        audit_id: str,
        *,
        reason: str,
        actor: str = "mcp_strategist",
    ) -> dict[str, Any]:
        target = next(
            (
                event
                for event in reversed(self._audit_events)
                if event.get("audit_id") == audit_id
            ),
            None,
        )
        if target is None:
            raise ValueError(f"audit_id not found: {audit_id}")

        if target.get("event_type") not in {"config_update", "config_rollback"}:
            raise ValueError("target audit event is not rollback-capable")

        model_name = target["model"]
        current_payload = self._configs.get(model_name)

        if current_payload is not None:
            await self.snapshotter.snapshot(
                model_name=model_name,
                payload=current_payload,
                source_audit_id=audit_id,
            )

        rollback_payload = self._validate(model_name, target["payload"])
        self._configs[model_name] = rollback_payload

        if self.redis_client is not None:
            await self.redis_client.delete(f"policy:{model_name}")

        rollback_event = {
            "audit_id": str(uuid4()),
            "event_type": "config_rollback",
            "model": model_name,
            "payload": rollback_payload,
            "rolled_back_to_audit_id": audit_id,
            "rollback_reason": reason,
            "actor": actor,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        await self._persist_audit(rollback_event)

        return rollback_event
