import os
from unittest.mock import AsyncMock, patch

import pytest

from cio.core.router import OutputRouter
from cio.models import (
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    MarketSignals,
    PnlTrend,
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


@pytest.mark.asyncio
async def test_output_router_shadow_mode():
    """
    Verifies that OutputRouter:
    1. Does NOT publish to NATS when DRY_RUN=true.
    2. ALWAYS calls vector_client.upsert regardless of DRY_RUN.
    """
    # 1. Setup Mock NATS Client and Vector Client
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        realtime_strategies_url="http://realtime",
    )

    # 2. Setup Context and Decision
    regime = RegimeResult(
        regime=RegimeEnum.RANGING,
        regime_confidence=ConfidenceLevel.MEDIUM,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="test",
        thought_trace="test",
    )

    context = TriggerContext(
        correlation_id="shadow-test-id",
        source_subject="test",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload={"symbol": "BTCUSDT", "side": "long"},
        regime=regime,
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="test",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="test",
        ),
        strategy_id="test_strat",
        strategy_stats=StrategyStats(recent_pnl_trend=PnlTrend.NEUTRAL),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=10000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(
            max_drawdown_pct=0.1,
            max_orders_global=50,
            max_orders_per_symbol=5,
            max_position_size_usd=1000.0,
        ),
    )

    decision = DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        computed_position_size_usd=1000.0,
        action=ActionType.EXECUTE,
        justification="Test",
        thought_trace="test",
    )

    # 3. Execution with DRY_RUN=true
    with patch.dict(os.environ, {"DRY_RUN": "true"}):
        await router.route(context, decision)

        # 4. Assertions
        mock_nc.publish.assert_not_called()
        mock_vc.upsert.assert_called_once()
        print(
            "✅ Verified: OutputRouter blocked NATS publish but performed Vector upsert in shadow mode."
        )

    # 5. Execution with DRY_RUN=false
    mock_nc.publish.reset_mock()
    mock_vc.upsert.reset_mock()
    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(context, decision)

        # Assertion: NATS publish SHOULD have been called (Twice for T-Junction)
        assert mock_nc.publish.call_count == 2
        mock_vc.upsert.assert_called_once()
        print(
            "✅ Verified: OutputRouter performed T-Junction NATS publish and Vector upsert in active mode."
        )


@pytest.mark.asyncio
async def test_output_router_defaults_to_active_mode_when_dry_run_unset(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    """Verifies the router publishes when DRY_RUN is unset and logs success once."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        realtime_strategies_url="http://realtime",
    )

    regime = RegimeResult(
        regime=RegimeEnum.RANGING,
        regime_confidence=ConfidenceLevel.MEDIUM,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="test",
        thought_trace="test",
    )

    context = TriggerContext(
        correlation_id="default-active-mode-id",
        source_subject="test",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload={"symbol": "BTCUSDT", "side": "long"},
        regime=regime,
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="test",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="test",
        ),
        strategy_id="test_strat",
        strategy_stats=StrategyStats(recent_pnl_trend=PnlTrend.NEUTRAL),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=10000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(
            max_drawdown_pct=0.1,
            max_orders_global=50,
            max_orders_per_symbol=5,
            max_position_size_usd=1000.0,
        ),
    )

    decision = DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        computed_position_size_usd=1000.0,
        action=ActionType.EXECUTE,
        justification="Test",
        thought_trace="test",
    )

    monkeypatch.delenv("DRY_RUN", raising=False)

    with caplog.at_level("INFO"):
        await router.route(context, decision)

    assert mock_nc.publish.call_count == 2
    mock_vc.upsert.assert_called_once()
    assert caplog.messages.count("T-Junction dispatch successful") == 1
