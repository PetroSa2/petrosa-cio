"""Deterministic Nurse policy enforcement."""

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class EnforcerResult:
    """Outcome of intent policy enforcement."""

    approved: bool
    reason: str | None = None


class NurseEnforcer:
    """Simple deterministic validation gate for incoming intent payloads."""

    VALID_ACTIONS = {"buy", "sell", "hold", "close"}

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

        return EnforcerResult(approved=True)
