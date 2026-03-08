import pytest
from unittest.mock import AsyncMock, MagicMock
from cio.core.context_builder import ContextBuilder
from cio.core.vector import MockVectorClient
from cio.models import (
    TriggerType, RegimeResult, RegimeEnum, ConfidenceLevel, VolatilityLevel,
    StrategyStats, PnlTrend, StrategyDefaults, PortfolioSummary, RiskLimits
)

@pytest.mark.asyncio
async def test_context_builder_cold_path_retrieval():
    """
    Verifies that COLD triggers result in historical context retrieval.
    """
    # 1. Setup
    mock_vector = AsyncMock(spec=MockVectorClient)
    mock_vector.query.return_value = "Seeded Historical Context"
    
    builder = ContextBuilder(
        data_manager_url="http://dm",
        tradeengine_url="http://te",
        strategy_api_url="http://sa",
        vector_client=mock_vector
    )
    
    # Use real models to satisfy Pydantic validation
    regime = RegimeResult(
        regime=RegimeEnum.RANGING,
        regime_confidence=ConfidenceLevel.MEDIUM,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="test",
        thought_trace="test"
    )
    stats = StrategyStats(recent_pnl_trend=PnlTrend.NEUTRAL)
    defaults = StrategyDefaults(stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24)
    portfolio = PortfolioSummary(net_directional_exposure=0.0, same_asset_pct=0.0, open_positions_count=0)
    risk = RiskLimits(max_drawdown_pct=0.1, max_orders_global=50, max_orders_per_symbol=5, max_position_size_usd=1000.0)

    # Mock the internal fetchers
    builder._fetch_regime = AsyncMock(return_value=regime)
    builder._fetch_portfolio_and_risk = AsyncMock(return_value=(portfolio, risk, {}))
    builder._fetch_strategy_data = AsyncMock(return_value=(stats, defaults))

    # 2. Test COLD trigger (SCHEDULED_REVIEW)
    ctx = await builder.build(
        correlation_id="test-cold",
        source_subject="review.test",
        trigger_type=TriggerType.SCHEDULED_REVIEW,
        payload={"symbol": "BTCUSDT", "strategy_id": "test_strat"}
    )
    
    # Assertions
    assert ctx.historical_context == "Seeded Historical Context"
    mock_vector.query.assert_called_once_with("test_strat")
    
    # 3. Test HOT trigger (TRADE_INTENT) - should NOT call vector
    mock_vector.query.reset_mock()
    ctx_hot = await builder.build(
        correlation_id="test-hot",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={"symbol": "BTCUSDT", "strategy_id": "test_strat"}
    )
    
    assert ctx_hot.historical_context is None
    mock_vector.query.assert_not_called()
    
    await builder.close()
