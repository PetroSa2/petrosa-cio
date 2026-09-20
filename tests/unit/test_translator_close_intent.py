"""Regression tests for petrosa_k8s#213 (P0): the translator must never
collapse an unrecognized or "close" trade side into "sell". A close
instruction inverted into a SELL order is one of the most severe
correctness failures possible in the execution path.

Root cause was a binary `else`:
    action = "buy" if side_lower in ("long", "buy", "bullish") else "sell"

Anything that was not an explicit long/buy/bullish alias — including
"close", "hold", typos, or a producer field-drift — silently became "sell".
This file locks in the fix: an explicit closed mapping that passes "close"
through untouched and REJECTS (returns None) anything it does not
recognize, rather than guessing a direction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# `contracts/` lives at the repo root alongside `cio/` and is not part of the
# installed `cio*` package, so it is not reliably importable under every
# pytest invocation mode. Make the import robust regardless of how the test
# runner was invoked (mirrors test_leverage_signal_propagation.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cio.models import (  # noqa: E402
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    MarketSignals,
    PortfolioSummary,
    RegimeEnum,
    RegimeFit,
    RegimeResult,
    RiskLimits,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)
from cio.output.translator import TradeEngineTranslator  # noqa: E402
from contracts.signal import Signal  # noqa: E402


def _make_context(**overrides) -> TriggerContext:
    regime = RegimeResult(
        regime=RegimeEnum.RANGING,
        regime_confidence=ConfidenceLevel.MEDIUM,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="test",
        thought_trace="test",
    )
    payload_overrides = overrides.pop("trigger_payload", None) or {}
    trigger_payload = {
        "symbol": "BTCUSDT",
        "current_price": 50000.0,
    }
    # Only default `side` in when the override doesn't already supply one of
    # the three direction keys the translator checks — otherwise a
    # leftover default "side" would mask an "action"/"signal_type" override
    # being tested (translator precedence is side > action > signal_type).
    if not any(k in payload_overrides for k in ("side", "action", "signal_type")):
        trigger_payload["side"] = "long"
    trigger_payload.update(payload_overrides)

    defaults = {
        "correlation_id": "test-corr",
        "source_subject": "cio.intent.trading.s1",
        "trigger_type": TriggerType.TRADE_INTENT,
        "trigger_payload": trigger_payload,
        "regime": regime,
        "volatility_level": VolatilityLevel.MEDIUM,
        "market_signals": MarketSignals(
            signal_summary="test",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.5,
            price_action_character="Neutral",
        ),
        "strategy_id": "s1",
        "strategy_stats": StrategyStats(),
        "strategy_defaults": StrategyDefaults(
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            leverage=1.0,
            max_hold_hours=24.0,
        ),
        "global_drawdown_pct": 0.0,
        "open_orders_global": 0,
        "open_orders_symbol": 0,
        "available_capital_usd": 1000.0,
        "portfolio": PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        "risk_limits": RiskLimits(
            max_single_position_pct=0.1,
            max_global_drawdown_pct=0.1,
            max_portfolio_exposure=0.5,
            max_open_orders=10,
            max_same_asset_concentration=0.25,
        ),
    }
    defaults.update(overrides)
    return TriggerContext(**defaults)


def _make_decision(position_usd: float = 100.0) -> DecisionResult:
    return DecisionResult(
        action=ActionType.EXECUTE,
        justification="test",
        thought_trace="test",
        computed_position_size_usd=position_usd,
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
    )


class TestLongAndShortStillMapCorrectly:
    """Non-regression: the happy path must be unaffected by the fix."""

    @pytest.mark.parametrize("alias", ["long", "buy", "bullish", "LONG", "Buy"])
    def test_long_aliases_map_to_buy(self, alias):
        ctx = _make_context(trigger_payload={"side": alias})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["action"] == "buy"
        # Round-trips through the actual Signal contract model tradeengine
        # validates against (extra="forbid" — see contracts/signal.py#599).
        assert Signal(**result).action == "buy"

    @pytest.mark.parametrize("alias", ["short", "sell", "bearish", "SHORT", "Sell"])
    def test_short_aliases_map_to_sell(self, alias):
        ctx = _make_context(trigger_payload={"side": alias})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["action"] == "sell"
        assert Signal(**result).action == "sell"


class TestCloseIsNeverInvertedToSell:
    """The core regression: petrosa_k8s#213."""

    @pytest.mark.parametrize("key", ["side", "action", "signal_type"])
    @pytest.mark.parametrize("value", ["close", "CLOSE", "Close"])
    def test_close_passes_through_as_close_regardless_of_source_key(self, key, value):
        ctx = _make_context(trigger_payload={key: value})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["action"] == "close"
        assert result["action"] != "sell"
        # Round-trips through the real consumer contract (which already
        # accepts "close" — the break was entirely on the CIO side).
        assert Signal(**result).action == "close"

    def test_close_long_preserves_direction_in_metadata(self):
        """Forward-compatible: if a producer is fixed to emit a
        direction-qualified close token, the direction is carried through
        (in `metadata`, since `Signal` has `extra="forbid"` at the top
        level — see contracts/signal.py#599) rather than lost again."""
        ctx = _make_context(trigger_payload={"side": "close_long"})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["action"] == "close"
        assert result["metadata"]["position_side"] == "long"
        assert Signal(**result).action == "close"

    def test_close_short_preserves_direction_in_metadata(self):
        ctx = _make_context(trigger_payload={"side": "close_short"})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["action"] == "close"
        assert result["metadata"]["position_side"] == "short"
        assert Signal(**result).action == "close"

    def test_plain_close_has_no_position_side(self):
        ctx = _make_context(trigger_payload={"side": "close"})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["metadata"]["position_side"] is None


class TestUnrecognizedActionIsRejectedNotDefaulted:
    """The other half of the fix: anything the translator does not
    recognize must be refused, never silently mapped to a direction."""

    @pytest.mark.parametrize(
        "value", ["hold", "HOLD", "unknown", "flatten", "", "typo_long", "cancel"]
    )
    def test_unrecognized_side_returns_none(self, value):
        ctx = _make_context(trigger_payload={"side": value})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is None

    def test_unrecognized_side_logs_contract_violation(self, caplog):
        import logging

        ctx = _make_context(trigger_payload={"side": "flatten"})
        with caplog.at_level(logging.CRITICAL):
            result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is None
        assert any("CONTRACT VIOLATION" in record.message for record in caplog.records)
