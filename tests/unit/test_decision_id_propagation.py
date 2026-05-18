"""Tests for decision_id propagation (P0.1a / petrosa_k8s#578)."""

from __future__ import annotations

from cio.models import (
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
from cio.output.translator import TradeEngineTranslator


def _make_context(**overrides) -> TriggerContext:
    regime = RegimeResult(
        regime=RegimeEnum.RANGING,
        regime_confidence=ConfidenceLevel.MEDIUM,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="test",
        thought_trace="test",
    )
    defaults = dict(
        correlation_id="test-corr",
        source_subject="cio.intent.trading.s1",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload={"symbol": "BTCUSDT", "side": "long", "current_price": 50000.0},
        regime=regime,
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="test",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.5,
            price_action_character="Neutral",
        ),
        strategy_id="s1",
        strategy_stats=StrategyStats(),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            leverage=1.0,
            max_hold_hours=24.0,
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=1000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(
            max_single_position_pct=0.1,
            max_global_drawdown_pct=0.1,
            max_portfolio_exposure=0.5,
            max_open_orders=10,
            max_same_asset_concentration=0.25,
        ),
    )
    defaults.update(overrides)
    return TriggerContext(**defaults)


def _make_decision(position_usd: float = 100.0) -> DecisionResult:
    from cio.models import ActionType

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


class TestDecisionIdInContext:
    def test_decision_id_auto_generated_when_not_provided(self):
        ctx = _make_context()
        assert ctx.decision_id
        assert len(ctx.decision_id) == 32

    def test_decision_id_uses_provided_value(self):
        ctx = _make_context(decision_id="abc123")
        assert ctx.decision_id == "abc123"

    def test_two_contexts_get_different_decision_ids(self):
        ctx1 = _make_context()
        ctx2 = _make_context()
        assert ctx1.decision_id != ctx2.decision_id


class TestDecisionIdInTranslator:
    def test_decision_id_in_top_level_signal(self):
        ctx = _make_context(decision_id="d1d2d3")
        decision = _make_decision()
        result = TradeEngineTranslator.to_legacy_signal(ctx, decision)
        assert result is not None
        assert result["decision_id"] == "d1d2d3"

    def test_decision_id_in_metadata(self):
        ctx = _make_context(decision_id="meta-test")
        decision = _make_decision()
        result = TradeEngineTranslator.to_legacy_signal(ctx, decision)
        assert result is not None
        assert result["metadata"]["decision_id"] == "meta-test"

    def test_decision_id_matches_context(self):
        ctx = _make_context()
        decision = _make_decision()
        result = TradeEngineTranslator.to_legacy_signal(ctx, decision)
        assert result is not None
        assert result["decision_id"] == ctx.decision_id
        assert result["metadata"]["decision_id"] == ctx.decision_id
