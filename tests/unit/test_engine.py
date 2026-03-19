import pytest

from cio.core.engine import CodeEngine
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


def build_test_context(drawdown=0.0, win_rate=0.6, capital=10000.0):
    """Helper to build a context for testing."""
    return TriggerContext(
        correlation_id="test",
        source_subject="test",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload={},
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="test",
            thought_trace="test",
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="test",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="test",
        ),
        strategy_id="test",
        strategy_stats=StrategyStats(
            win_rate=win_rate, recent_pnl_trend=PnlTrend.NEUTRAL
        ),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=drawdown,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=capital,
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


def test_code_engine_risk_gate_drawdown():
    # Context with drawdown above limit
    ctx = build_test_context(drawdown=0.15)
    result = CodeEngine.run(ctx)
    assert result.hard_blocked is True
    assert "drawdown" in result.block_reason


def test_code_engine_ev_calculation():
    # default regime is RANGING (0.8x TP multiplier)
    # win_rate=0.6, TP=0.04 * 0.8 = 0.032, SL=0.02 (adj for Medium volatility 1.2x -> 0.024)
    # EV = (0.6 * 0.032) - (0.4 * 0.024) = 0.0192 - 0.0096 = 0.0096
    ctx = build_test_context(win_rate=0.6)
    result = CodeEngine.run(ctx)
    assert result.hard_blocked is False
    assert pytest.approx(result.gross_ev, 0.0001) == 0.0096


def test_code_engine_kelly_sizing():
    ctx = build_test_context(win_rate=0.6)
    result = CodeEngine.run(ctx)
    # Kelly fraction capped at 0.25
    assert result.kelly_fraction <= 0.25
    assert result.kelly_position_usd <= ctx.risk_limits.max_position_size_usd


def test_code_engine_regime_adjustment():
    """Verifies TP multiplier is applied and impacts EV calculation."""
    ctx = build_test_context(win_rate=0.6)
    ctx.regime.regime = RegimeEnum.TRENDING_BULL  # 1.3x TP multiplier
    ctx.volatility_level = VolatilityLevel.MEDIUM  # 1.2x SL multiplier

    result = CodeEngine.run(ctx)

    # Initial TP 0.04 * 1.3 = 0.052
    assert pytest.approx(result.recommended_tp_pct, 0.0001) == 0.052
    # Initial SL 0.02 * 1.2 = 0.024
    assert pytest.approx(result.recommended_sl_pct, 0.0001) == 0.024

    # EV = (0.6 * 0.052) - (0.4 * 0.024) = 0.0312 - 0.0096 = 0.0216
    assert pytest.approx(result.gross_ev, 0.0001) == 0.0216


def test_code_engine_regime_confidence_bypass():
    """Verifies that hard blocks are bypassed when regime confidence is low."""
    ctx = build_test_context()
    ctx.regime.regime = RegimeEnum.CHOPPY

    # High confidence -> Should block
    ctx.regime.regime_confidence = ConfidenceLevel.HIGH
    result = CodeEngine.run(ctx)
    assert result.hard_blocked is True

    # Low confidence -> Should NOT block (bypass fix)
    ctx.regime.regime_confidence = ConfidenceLevel.LOW
    result = CodeEngine.run(ctx)
    assert result.hard_blocked is False
