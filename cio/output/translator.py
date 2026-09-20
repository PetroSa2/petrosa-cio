import logging
from datetime import UTC, datetime
from typing import Any

from cio.models import DecisionResult, TriggerContext

logger = logging.getLogger(__name__)

# petrosa_k8s#213 (P0): explicit side->action mapping. This MUST stay a
# closed lookup, never a binary `if/else`, because a binary else silently
# folds every unrecognized/close token into "sell" — inverting a close
# instruction into a position-opening SELL order. Any token not in this map
# is a CONTRACT VIOLATION and is rejected (see `to_legacy_signal` below),
# never defaulted to a direction.
_LONG_ALIASES = frozenset({"long", "buy", "bullish"})
_SHORT_ALIASES = frozenset({"short", "sell", "bearish"})
# Plain "close" carries no direction by the time it reaches CIO — producers
# (e.g. petrosa-realtime-strategies) currently collapse CLOSE_LONG/
# CLOSE_SHORT into a single "close" token before this payload is built
# (petrosa_k8s#213 root-cause chain, step 1). If/when a producer is fixed to
# emit direction-qualified tokens, `_CLOSE_WITH_DIRECTION` below already
# forwards `position_side` end-to-end without any change needed here.
_CLOSE_ALIASES = frozenset({"close"})
_CLOSE_WITH_DIRECTION: dict[str, str] = {
    "close_long": "long",
    "close_short": "short",
}


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
        - action: Maps to 'buy'/'sell'/'close' via an explicit closed lookup
          (petrosa_k8s#213). Never a binary else — any token not recognized
          is a CONTRACT VIOLATION and translation is refused (returns None)
          rather than defaulting to a direction.
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
            # petrosa_k8s#213: explicit closed mapping — never a binary
            # else. Unrecognized tokens are rejected (CONTRACT VIOLATION),
            # never silently defaulted to a directional order.
            side_lower = str(side).lower()
            position_side: str | None = None
            if side_lower in _LONG_ALIASES:
                action = "buy"
            elif side_lower in _SHORT_ALIASES:
                action = "sell"
            elif side_lower in _CLOSE_ALIASES:
                action = "close"
            elif side_lower in _CLOSE_WITH_DIRECTION:
                action = "close"
                position_side = _CLOSE_WITH_DIRECTION[side_lower]
            else:
                logger.critical(
                    "CONTRACT VIOLATION: Unrecognized trade side/action — "
                    "refusing to guess a direction",
                    extra={
                        "correlation_id": correlation_id,
                        "side": side,
                        "side_lower": side_lower,
                        "payload_keys": list(context.trigger_payload.keys()),
                    },
                )
                return None

            # CRITICAL FIX: Convert USD position size to base asset quantity
            base_quantity = quantity_usd / current_price
            logger.debug(
                f"Translation math: ${quantity_usd} / {current_price} = {base_quantity} assets",
                extra={"correlation_id": correlation_id},
            )

            # petrosa-cio#214: forward the trigger's real confidence and
            # timeframe instead of hardcoding. Both producers (ta_bot,
            # realtime-strategies) already set these on every signal
            # (ta_bot/models/signal.py, strategies/models/signals.py), but
            # this translator previously pinned confidence to a constant
            # 0.9 and omitted timeframe entirely — tradeengine then applied
            # its own default (contracts/signal.py: timeframe="1h") and
            # signal_aggregator.py scored EVERY CIO-routed signal a
            # constant 0.9*0.7=0.63 regardless of the strategy's actual
            # timeframe, making per-timeframe weighting inert.
            # `contracts.signal.Signal.confidence` is a REQUIRED float
            # (0<=v<=1, no default) — unlike the arbiter's own tolerant
            # default (`listener.py`'s `payload.get("confidence", 0.5)`),
            # we must always emit a concrete, in-range value here or the
            # downstream Signal(**result) construction raises.
            raw_confidence = context.trigger_payload.get("confidence", 0.5)
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                logger.warning(
                    "Non-numeric confidence %r in trigger_payload; defaulting to 0.5",
                    raw_confidence,
                    extra={"correlation_id": correlation_id},
                )
                confidence = 0.5
            # Defensive clamp: producers already validate confidence via
            # their own Signal model (ge=0, le=1) before publish, but never
            # trust an upstream contract to hold forever — Signal(**result)
            # would otherwise raise ValueError on an out-of-range value.
            confidence = max(0.0, min(1.0, confidence))
            # "1h" mirrors contracts/signal.py's own default so a payload
            # that genuinely omits timeframe behaves exactly as before
            # (silent fallback, not a new hardcode) — only the previous
            # unconditional omission is fixed.
            timeframe = context.trigger_payload.get("timeframe") or "1h"

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
                "confidence": confidence,
                "source": "petrosa-cio",
                "strength": "strong",
                "strategy_mode": "llm_reasoning",
                "timeframe": timeframe,
                "timestamp": datetime.now(UTC).isoformat(),
                "decision_id": context.decision_id,
                # Map risk management parameters from decision (primary) or payload
                # P1.5-AC3 (#137) / #174 — admission-time leverage decided by
                # CIO's leverage arbiter. `decision.decided_leverage` is set
                # unconditionally by `OutputRouter.route` before this
                # translator runs; None here only for hand-built
                # DecisionResult objects that bypass the router (e.g. direct
                # unit-test construction).
                "leverage": decision.decided_leverage,
                "stop_loss": context.trigger_payload.get("stop_loss"),
                "stop_loss_pct": decision.stop_loss_pct
                or context.trigger_payload.get("stop_loss_pct"),
                "take_profit": context.trigger_payload.get("take_profit"),
                "take_profit_pct": decision.take_profit_pct
                or context.trigger_payload.get("take_profit_pct"),
                "metadata": {
                    "correlation_id": correlation_id,
                    "decision_id": context.decision_id,
                    "cio_justification": decision.justification,
                    "thought_trace": decision.thought_trace,
                    "original_size_usd": quantity_usd,
                    # petrosa_k8s#213: forwarded only when the producer sent
                    # a direction-qualified close token (e.g. "close_long");
                    # None for plain "close" (direction unknown at this hop)
                    # and for buy/sell. Lives in `metadata` (untyped dict),
                    # not top-level, because petrosa-tradeengine's Signal
                    # contract has `extra="forbid"` (#599) — a new top-level
                    # field would hard-reject every signal until the
                    # consumer contract is updated to declare it.
                    "position_side": position_side,
                },
            }

            return legacy_signal

        except Exception as e:
            logger.error(
                f"Translation failure: {e}", extra={"correlation_id": correlation_id}
            )
            return None
