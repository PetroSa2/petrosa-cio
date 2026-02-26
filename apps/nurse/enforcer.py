"""Deterministic Nurse policy enforcement."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from apps.nurse.guard import RegimeGuard


@dataclass(slots=True)
class EnforcerResult:
    """Outcome of intent policy enforcement."""

    approved: bool
    reason: str | None = None
    metadata: dict[str, Any] | None = None


class ConfigSnapshotter:
    """Stores pre-change snapshots for rollback workflows."""

    def __init__(self, history_collection: Any | None = None):
        self.history_collection = history_collection

    async def snapshot(
        self,
        *,
        model_name: str,
        payload: dict[str, Any],
        source_audit_id: str | None = None,
    ) -> dict[str, Any]:
        document = {
            "model": model_name,
            "payload": payload,
            "source_audit_id": source_audit_id,
            "snapshot_at": datetime.now(UTC).isoformat(),
        }
        if self.history_collection is not None:
            await self.history_collection.insert_one(document)
        return document


class NurseEnforcer:
    """Simple deterministic validation gate for incoming intent payloads."""

    VALID_ACTIONS = {"buy", "sell", "hold", "close"}

    def __init__(self, regime_guard: RegimeGuard | None = None):
        self.regime_guard = regime_guard or RegimeGuard()

    @staticmethod
    def scale_position_size(
        intent_payload: dict[str, Any], scale_factor: float
    ) -> float:
        quantity = float(intent_payload.get("quantity", 0.0))
        return quantity * scale_factor

    async def enforce(self, intent_payload: dict[str, Any]) -> EnforcerResult:
        action = str(intent_payload.get("action", "")).lower()
        if action not in self.VALID_ACTIONS:
            return EnforcerResult(approved=False, reason="invalid_action")

        confidence = intent_payload.get("confidence")
        if confidence is not None:
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError):
                return EnforcerResult(approved=False, reason="invalid_confidence")
            if not 0.0 <= confidence_value <= 1.0:
                return EnforcerResult(approved=False, reason="invalid_confidence")

        guard_decision = await self.regime_guard.evaluate(intent_payload)
        metadata = dict(guard_decision.metadata)
        metadata["saved_capital"] = guard_decision.saved_capital

        if guard_decision.scale_factor < 1.0:
            metadata["scaled_quantity"] = self.scale_position_size(
                intent_payload, guard_decision.scale_factor
            )
            metadata["scale_factor"] = guard_decision.scale_factor

        if not guard_decision.approved:
            metadata["veto_type"] = "semantic"
            return EnforcerResult(
                approved=False,
                reason=guard_decision.veto_reason,
                metadata=metadata,
            )

        return EnforcerResult(approved=True, metadata=metadata)
