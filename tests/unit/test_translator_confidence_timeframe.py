"""Regression tests for petrosa-cio#214 (defect 1): the translator must
forward the trigger's real `confidence` and `timeframe` instead of
hardcoding `confidence: 0.9` and omitting `timeframe` entirely.

Root cause: `translator.py` built the legacy payload with a hardcoded
`"confidence": 0.9` and no `timeframe` key at all, despite both producers
(ta_bot, realtime-strategies) setting both fields on every signal. Since
tradeengine defaults a missing `timeframe` to `"1h"`
(`contracts/signal.py`), and `signal_aggregator.py` scores
`base_strength * timeframe_weights.get(signal.timeframe, 1.0)`, every
CIO-routed signal scored a constant `0.9 * 0.7 = 0.63` regardless of its
actual timeframe or confidence — aggregation ranking and per-timeframe
weighting were inert for CIO-routed signals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from petrosa_contracts import Signal  # noqa: E402

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
        "side": "long",
    }
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


class TestConfidenceForwarded:
    @pytest.mark.parametrize("confidence", [0.0, 0.15, 0.42, 0.9, 1.0])
    def test_real_confidence_is_forwarded_not_hardcoded(self, confidence):
        ctx = _make_context(trigger_payload={"confidence": confidence})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["confidence"] == confidence
        assert Signal(**result).confidence == confidence

    def test_missing_confidence_falls_back_to_half_not_point_nine(self):
        """No hardcoded 0.9: the fallback for a payload that genuinely
        omits confidence is the same conservative 0.5 default the arbiter
        already uses (`listener.py`), not the old fabricated 0.9."""
        ctx = _make_context(trigger_payload={})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["confidence"] == 0.5
        assert result["confidence"] != 0.9

    def test_non_numeric_confidence_falls_back_to_half(self, caplog):
        import logging

        ctx = _make_context(trigger_payload={"confidence": "not-a-number"})
        with caplog.at_level(logging.WARNING):
            result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["confidence"] == 0.5
        assert "Non-numeric confidence" in caplog.text

    @pytest.mark.parametrize(("raw", "expected"), [(-0.5, 0.0), (1.5, 1.0)])
    def test_out_of_range_confidence_is_clamped(self, raw, expected):
        ctx = _make_context(trigger_payload={"confidence": raw})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["confidence"] == expected
        # Round-trips through the real consumer contract, which rejects
        # out-of-range confidence outright (ge=0, le=1).
        assert Signal(**result).confidence == expected


class TestTimeframeForwarded:
    @pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h"])
    def test_real_timeframe_is_forwarded(self, timeframe):
        ctx = _make_context(trigger_payload={"timeframe": timeframe})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["timeframe"] == timeframe
        assert Signal(**result).timeframe == timeframe

    def test_missing_timeframe_falls_back_to_1h_matching_contract_default(self):
        """The translator previously omitted `timeframe` entirely, letting
        `contracts.signal.Signal` silently apply its own "1h" default. The
        fallback here reproduces that exact behavior for payloads that
        truly lack a timeframe — only the case where the payload DOES carry
        one (and it was previously dropped) is fixed."""
        ctx = _make_context(trigger_payload={})
        result = TradeEngineTranslator.to_legacy_signal(ctx, _make_decision())

        assert result is not None
        assert result["timeframe"] == "1h"

    def test_1m_and_1h_signals_produce_different_confidence_and_timeframe(self):
        """petrosa-cio#214 AC: two signals on different timeframes must
        carry different `timeframe` values end-to-end (the CIO-side half of
        "two signals on different timeframes produce different scores" —
        the scoring itself happens in tradeengine's signal_aggregator,
        which is a different repo/service and out of this ticket's scope)."""
        ctx_1m = _make_context(trigger_payload={"timeframe": "1m", "confidence": 0.8})
        ctx_1h = _make_context(trigger_payload={"timeframe": "1h", "confidence": 0.8})
        result_1m = TradeEngineTranslator.to_legacy_signal(ctx_1m, _make_decision())
        result_1h = TradeEngineTranslator.to_legacy_signal(ctx_1h, _make_decision())

        assert result_1m is not None
        assert result_1h is not None
        assert result_1m["timeframe"] == "1m"
        assert result_1h["timeframe"] == "1h"
        assert result_1m["timeframe"] != result_1h["timeframe"]
