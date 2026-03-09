import logging
from datetime import datetime
from typing import Any

from cio.models import DecisionResult, TriggerContext

logger = logging.getLogger(__name__)


class TradeEngineTranslator:
    """
    Translates CIO DecisionResult into the legacy Signal model
    expected by petrosa-tradeengine.
    """

    @staticmethod
    def to_legacy_signal(
        context: TriggerContext, decision: DecisionResult
    ) -> dict[str, Any] | None:
        """
        Maps new domain models to legacy Signal JSON structure.

        Mapping Rules:
        - action: Maps to 'buy' or 'sell' based on payload side.
        - quantity: Maps to base asset quantity (USD / current_price).
        - price: Maps to current_price from market_signals.
        - source: Fixed as 'petrosa-cio'.
        """
        correlation_id = context.correlation_id

        try:
            # 1. Critical Field Validation
            # Support multiple possible keys for the trade direction
            side = (
                context.trigger_payload.get("side")
                or context.trigger_payload.get("action")
                or context.trigger_payload.get("signal_type")
            )

            current_price = context.market_signals.current_price
            quantity_usd = decision.computed_position_size_usd

            if not side or current_price <= 0 or quantity_usd is None:
                logger.critical(
                    "CONTRACT VIOLATION: Missing critical fields for translation",
                    extra={
                        "correlation_id": correlation_id,
                        "has_side": bool(side),
                        "price": current_price,
                        "quantity_usd": quantity_usd,
                        "payload_keys": list(context.trigger_payload.keys()),
                    },
                )
                return None

            # 2. Action Mapping and Quantity Translation
            side_lower = str(side).lower()
            action = "buy" if side_lower in ("long", "buy", "bullish") else "sell"

            # CRITICAL FIX: Convert USD position size to base asset quantity
            base_quantity = quantity_usd / current_price
            logger.debug(
                f"Translation math: ${quantity_usd} / {current_price} = {base_quantity} assets",
                extra={"correlation_id": correlation_id},
            )

            # 3. Build Legacy Payload
            # Matching petrosa-tradeengine/contracts/signal.py Signal model
            legacy_signal = {
                "strategy_id": context.strategy_id,
                "strategy": context.strategy_id,
                "symbol": context.trigger_payload.get("symbol", "UNKNOWN"),
                "action": action,
                "price": current_price,
                "current_price": current_price,
                "quantity": base_quantity,
                "confidence": 0.9,
                "source": "petrosa-cio",
                "strength": "strong",
                "strategy_mode": "llm_reasoning",
                "timestamp": datetime.utcnow().isoformat(),
                # Map risk management parameters from payload
                "stop_loss": context.trigger_payload.get("stop_loss"),
                "stop_loss_pct": context.trigger_payload.get("stop_loss_pct"),
                "take_profit": context.trigger_payload.get("take_profit"),
                "take_profit_pct": context.trigger_payload.get("take_profit_pct"),
                "metadata": {
                    "correlation_id": correlation_id,
                    "cio_justification": decision.justification,
                    "thought_trace": decision.thought_trace,
                    "original_size_usd": quantity_usd,
                },
            }

            return legacy_signal

        except Exception as e:
            logger.error(
                f"Translation failure: {e}", extra={"correlation_id": correlation_id}
            )
            return None
