from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.orchestrator import Orchestrator
from cio.models import (
    ActionType,
    ConfidenceLevel,
    MarketSignals,
    PortfolioSummary,
    RegimeEnum,
    RegimeResult,
    RiskLimits,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)


def _context(action: str) -> TriggerContext:
    return TriggerContext(
        correlation_id="non-actionable-test",
        trigger_type=TriggerType.TRADE_INTENT,
        strategy_id="test-strategy",
        symbol="BTCUSDT",
        source_subject="cio.intent.trading.test-strategy",
        trigger_payload={"action": action, "symbol": "BTCUSDT"},
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.HIGH,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="test",
            thought_trace="test",
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="test",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.5,
            price_action_character="neutral",
        ),
        strategy_stats=StrategyStats(),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=1000.0,
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


@pytest.mark.asyncio
async def test_hold_short_circuits_llm_and_increments_counter():
    client = MagicMock()
    client.complete = AsyncMock()
    with patch("cio.core.metrics.NON_ACTIONABLE_INTENTS") as counter:
        decision = await Orchestrator(llm_client=client).run(_context("hold"))

    assert decision.action == ActionType.SKIP
    assert decision.justification == "NON_ACTIONABLE_INTENT"
    client.complete.assert_not_called()
    counter.add.assert_called_once_with(
        1, {"strategy_id": "test-strategy", "token": "hold"}
    )


@pytest.mark.asyncio
async def test_buy_still_enters_reasoning_loop():
    orchestrator = Orchestrator(llm_client=MagicMock())
    orchestrator.action_classifier.classify = AsyncMock()
    with (
        patch(
            "cio.core.orchestrator.apply_context_gate", new=AsyncMock(return_value=None)
        ),
        patch("cio.core.orchestrator.CodeEngine.run") as run_engine,
    ):
        run_engine.return_value = MagicMock(
            hard_blocked=True, block_context_fallback=True
        )
        await orchestrator.run(_context("buy"))

    run_engine.assert_called_once()
