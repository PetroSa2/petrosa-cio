"""Admission-time wiring between Orchestrator and PositionReviewLoop (#175).

FR60/P1.4-AC7 was implemented (#135) but the loop was never instantiated and
had no runner — this is the counterpart AC: once a position is actually
admitted (portfolio_tracker.record_admit fires), the same admission moment
must register the position with PositionReviewLoop so it enters the cadence.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.orchestrator import Orchestrator
from cio.core.portfolio_tracker import PortfolioTracker
from cio.models import (
    ConfidenceLevel,
    MarketSignals,
    PnlTrend,
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


def _make_context(strategy_id: str = "momentum-v3") -> TriggerContext:
    return TriggerContext(
        correlation_id="test-admission",
        trigger_type=TriggerType.TRADE_INTENT,
        strategy_id=strategy_id,
        symbol="BTCUSDT",
        source_subject="cio.intent.trading.momentum-v3",
        trigger_payload={"symbol": "BTCUSDT"},
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.HIGH,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="test",
            confidence=1.0,
            fit="good",
            thought_trace="test",
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="bullish",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.7,
            price_action_character="stable",
        ),
        strategy_stats=StrategyStats(recent_pnl_trend=PnlTrend.NEUTRAL),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=100_000.0,
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
async def test_admission_registers_position_with_review_loop():
    """A real admission (kelly_position_usd > 0, within ceiling) must call
    position_review_loop.add_position with the strategy_id."""
    with patch.dict(os.environ, {"NURSE_USE_LLM_REASONING": "false"}):
        with (
            patch("cio.core.orchestrator.CodeEngine") as MockEngine,
            patch("cio.core.orchestrator.ActionClassifier") as MockClassifier,
        ):
            mock_code_result = MagicMock()
            mock_code_result.hard_blocked = False
            mock_code_result.kelly_position_usd = 500.0
            MockEngine.run.return_value = mock_code_result

            mock_decision = MagicMock()
            MockClassifier.return_value.classify = AsyncMock(return_value=mock_decision)

            fresh_tracker = PortfolioTracker()
            position_review_loop = MagicMock()

            orchestrator = Orchestrator(
                portfolio_tracker=fresh_tracker,
                position_review_loop=position_review_loop,
            )

            await orchestrator.run(_make_context("momentum-v3"))

            position_review_loop.add_position.assert_called_once_with(
                "momentum-v3", "momentum-v3"
            )


@pytest.mark.asyncio
async def test_no_admission_no_registration():
    """kelly_position_usd == 0 → no admission → add_position must not be called."""
    with patch.dict(os.environ, {"NURSE_USE_LLM_REASONING": "false"}):
        with (
            patch("cio.core.orchestrator.CodeEngine") as MockEngine,
            patch("cio.core.orchestrator.ActionClassifier") as MockClassifier,
        ):
            mock_code_result = MagicMock()
            mock_code_result.hard_blocked = False
            mock_code_result.kelly_position_usd = 0.0
            MockEngine.run.return_value = mock_code_result

            MockClassifier.return_value.classify = AsyncMock(return_value=MagicMock())

            fresh_tracker = PortfolioTracker()
            position_review_loop = MagicMock()

            orchestrator = Orchestrator(
                portfolio_tracker=fresh_tracker,
                position_review_loop=position_review_loop,
            )

            await orchestrator.run(_make_context("momentum-v3"))

            position_review_loop.add_position.assert_not_called()


@pytest.mark.asyncio
async def test_admission_without_position_review_loop_does_not_raise():
    """Legacy construction (position_review_loop=None, the default) must still work."""
    with patch.dict(os.environ, {"NURSE_USE_LLM_REASONING": "false"}):
        with (
            patch("cio.core.orchestrator.CodeEngine") as MockEngine,
            patch("cio.core.orchestrator.ActionClassifier") as MockClassifier,
        ):
            mock_code_result = MagicMock()
            mock_code_result.hard_blocked = False
            mock_code_result.kelly_position_usd = 500.0
            MockEngine.run.return_value = mock_code_result

            MockClassifier.return_value.classify = AsyncMock(return_value=MagicMock())

            fresh_tracker = PortfolioTracker()
            orchestrator = Orchestrator(portfolio_tracker=fresh_tracker)
            assert orchestrator.position_review_loop is None

            # Must not raise.
            await orchestrator.run(_make_context("momentum-v3"))
