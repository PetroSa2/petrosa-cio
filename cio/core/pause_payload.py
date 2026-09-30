"""Payload builders for strategy pause requests."""

from typing import Any


def build_ta_bot_pause_payload(
    current_config: dict[str, Any], strategy_id: str, reason: str
) -> dict[str, Any]:
    """Build the bot-ta-analysis application-config pause request.

    Strategy-level config accepts strategy parameters only; ``enabled`` is an
    application setting. Removing the strategy from the current enabled list
    keeps the request compatible with ``AppConfigUpdateRequest`` while leaving
    every unrelated application setting untouched by the API's merge logic.
    """
    enabled = current_config.get("enabled_strategies", [])
    if not isinstance(enabled, list):
        raise ValueError("target application config has invalid enabled_strategies")

    return {
        "enabled_strategies": [item for item in enabled if item != strategy_id],
        "changed_by": f"petrosa-cio:{strategy_id}",
        "reason": reason,
        "validate_only": False,
    }
