import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.orchestrator import Orchestrator
from cio.models import (
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
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
from cio.models.decision import DecisionResult


@pytest.mark.asyncio
async def test_orchestrator_deterministic_bypass():
    """
    Verifies that the Orchestrator skips LLM personas when NURSE_USE_LLM_REASONING=false.
    """
    # 1. Setup - Mocking environment and personas
    with patch.dict(os.environ, {"NURSE_USE_LLM_REASONING": "false"}):
        # We need to mock the personas to ensure they are NOT called
        with (
            patch("cio.core.orchestrator.RegimeAnalyst") as MockRegime,
            patch("cio.core.orchestrator.StrategyAssessor") as MockStrategy,
            patch("cio.core.orchestrator.ActionClassifier") as MockClassifier,
        ):
            mock_classifier = MockClassifier.return_value
            mock_classifier.classify = AsyncMock()

            # Instantiate Orchestrator (it will read the env var)
            orchestrator = Orchestrator()
            assert orchestrator.use_llm_reasoning is False

            # Create a mock trigger context with ALL required fields
            context = TriggerContext(
                correlation_id="test-bypass",
                trigger_type=TriggerType.TRADE_INTENT,
                strategy_id="test_strat",
                symbol="BTCUSDT",
                source_subject="intent.test",
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

            # 2. Run
            await orchestrator.run(context)

            # 3. Assertions
            MockRegime.return_value.classify.assert_not_called()
            MockStrategy.return_value.assess.assert_not_called()

            # Classifier should be called with bypass_mode=True
            mock_classifier.classify.assert_called_once()
            args, kwargs = mock_classifier.classify.call_args
            assert kwargs["bypass_mode"] is True
            # Verify explicit bypass placeholders were used
            assert args[2].thought_trace == "DETERMINISTIC_BYPASS"  # RegimeResult
            assert args[3].thought_trace == "DETERMINISTIC_BYPASS"  # StrategyResult


@pytest.mark.asyncio
async def test_orchestrator_hard_block_with_bypass():
    """
    Verifies that hard blocks also respect the bypass_mode flag for the classifier.
    """
    with patch.dict(os.environ, {"NURSE_USE_LLM_REASONING": "false"}):
        with (
            patch("cio.core.orchestrator.CodeEngine") as MockEngine,
            patch("cio.core.orchestrator.ActionClassifier") as MockClassifier,
        ):
            # Mock hard block
            mock_code_result = MagicMock()
            mock_code_result.hard_blocked = True
            mock_code_result.block_reason = "Test Block"
            MockEngine.run.return_value = mock_code_result

            mock_classifier = MockClassifier.return_value
            mock_classifier.classify = AsyncMock()

            orchestrator = Orchestrator()
            context = TriggerContext(
                correlation_id="test-block",
                trigger_type=TriggerType.TRADE_INTENT,
                strategy_id="test_strat",
                symbol="BTCUSDT",
                source_subject="intent.test",
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

            await orchestrator.run(context)

            # Classifier should be called with bypass_mode=True because reasoning is disabled
            mock_classifier.classify.assert_called_once()
            _, kwargs = mock_classifier.classify.call_args
            assert kwargs["bypass_mode"] is True


def _make_bypass_context(regime_enum):
    """Helper: build a TriggerContext with the given regime for bypass tests."""
    return TriggerContext(
        correlation_id="test-regime-bypass",
        trigger_type=TriggerType.TRADE_INTENT,
        strategy_id="test_strat",
        symbol="BTCUSDT",
        source_subject="cio.intent.trading.test_strat",
        trigger_payload={"symbol": "BTCUSDT"},
        regime=RegimeResult(
            regime=regime_enum,
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
@pytest.mark.parametrize(
    "blocking_regime", [RegimeEnum.CHOPPY, RegimeEnum.CAPITULATION]
)
async def test_bypass_mode_skips_regime_hard_block(blocking_regime):
    """
    Verifies that bypass mode (NURSE_USE_LLM_REASONING=false) does NOT hard-block
    on CHOPPY or CAPITULATION regimes.  Risk-gate blocks (drawdown / orders) still
    apply; only policy-based regime blocks are skipped.
    """
    with patch.dict(os.environ, {"NURSE_USE_LLM_REASONING": "false"}):
        with patch("cio.core.orchestrator.ActionClassifier") as MockClassifier:
            mock_result = DecisionResult(
                hard_blocked=False,
                ev_passes=True,
                cost_viable=True,
                regime_confidence=ConfidenceLevel.HIGH,
                regime_fit=RegimeFit.GOOD,
                strategy_health=HealthStatus.HEALTHY,
                activation_recommendation=ActivationRecommendation.RUN,
                action=ActionType.EXECUTE,
                justification="bypass test",
                thought_trace="bypass",
            )
            MockClassifier.return_value.classify = AsyncMock(return_value=mock_result)

            orchestrator = Orchestrator()
            context = _make_bypass_context(blocking_regime)

            decision = await orchestrator.run(context)

            # Must NOT be blocked — bypass mode overrides regime policy blocks
            assert decision.action == ActionType.EXECUTE, (
                f"Expected EXECUTE in bypass mode with {blocking_regime} regime, "
                f"got {decision.action}"
            )
            # Classifier must be called with bypass_mode=True
            call_kwargs = MockClassifier.return_value.classify.call_args[1]
            assert call_kwargs["bypass_mode"] is True
