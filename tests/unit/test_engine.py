import pytest

from cio.core.engine import CodeEngine
from cio.models import (
    ConfidenceLevel,
    ContextGap,
    MarketSignals,
    MarketState,
    PnlTrend,
    PortfolioState,
    PortfolioSummary,
    PreDecisionContext,
    RegimeEnum,
    RegimeResult,
    RiskLimits,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)


def build_test_context(
    drawdown=0.0, win_rate=0.6, capital=10000.0, portfolio_state_available=None
):
    """Helper to build a context for testing.

    ``portfolio_state_available``: when not None, attaches a
    PreDecisionContext with ``portfolio_state_available`` set accordingly,
    exercising the #172 context-fetch-fallback provenance flag on
    CodeEngineResult.block_context_fallback.
    """
    pre_decision_context = None
    if portfolio_state_available is not None:
        market_state = MarketState(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            current_price=50000.0,
            primary_signal="test",
        )
        portfolio_state = PortfolioState(
            gross_exposure=0.0,
            same_asset_pct=0.0,
            open_positions_count=0,
            global_drawdown_pct=drawdown,
            available_capital_usd=capital,
            open_orders_global=0,
            open_orders_symbol=0,
        )
        pre_decision_context = PreDecisionContext(
            market_state=market_state,
            portfolio_state=portfolio_state,
            portfolio_state_available=portfolio_state_available,
        )
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
        pre_decision_context=pre_decision_context,
    )


def test_code_engine_risk_gate_drawdown():
    # Context with drawdown above limit
    ctx = build_test_context(drawdown=0.15)
    result = CodeEngine.run(ctx)
    assert result.hard_blocked is True
    assert "drawdown" in result.block_reason
    # No pre_decision_context attached (legacy/test-only path) -> defaults
    # to "assume real" rather than silently flagging every un-instrumented
    # caller as a fallback.
    assert result.block_context_fallback is False


def test_code_engine_risk_gate_context_fallback_flagged(caplog):
    """#172 — a hard block caused by ContextBuilder's safe-default fallback
    (portfolio_state_available=False) must be flagged as
    block_context_fallback=True and logged distinctly from a real breach,
    so operators never misdiagnose a context-fetch outage as a real
    drawdown breach again."""
    ctx = build_test_context(drawdown=0.15, portfolio_state_available=False)
    with caplog.at_level("WARNING"):
        result = CodeEngine.run(ctx)
    assert result.hard_blocked is True
    assert result.block_context_fallback is True
    assert any("FALLBACK" in record.message for record in caplog.records)


def test_code_engine_risk_gate_real_breach_not_flagged_as_fallback(caplog):
    """#172 — a hard block backed by live portfolio/risk data
    (portfolio_state_available=True) must NOT be flagged as a
    context-fetch fallback."""
    ctx = build_test_context(drawdown=0.15, portfolio_state_available=True)
    with caplog.at_level("WARNING"):
        result = CodeEngine.run(ctx)
    assert result.hard_blocked is True
    assert result.block_context_fallback is False
    assert any(
        "live portfolio/risk data" in record.message for record in caplog.records
    )


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


def test_code_engine_uses_absolute_order_payoff():
    ctx = build_test_context(win_rate=0.4)
    ctx.trigger_payload = {
        "entry_price": 100.0,
        "stop_loss": 99.78,
        "take_profit": 100.44,
    }

    result = CodeEngine.run(ctx)

    assert result.recommended_sl_pct == pytest.approx(0.0022)
    assert result.recommended_tp_pct == pytest.approx(0.0044)
    assert result.gross_ev == pytest.approx(0.00044)
    assert result.kelly_fraction == pytest.approx(0.1)


def test_carried_stop_wins_even_without_a_carried_target():
    ctx = build_test_context(win_rate=0.6)
    ctx.trigger_payload = {"entry_price": 100.0, "stop_loss": 99.78}

    result = CodeEngine.run(ctx)

    assert result.recommended_sl_pct == pytest.approx(0.0022)  # carried, not 2% x vol
    assert result.recommended_tp_pct == pytest.approx(0.032)  # configured 4% x regime


def test_carried_target_wins_even_without_a_carried_stop():
    ctx = build_test_context(win_rate=0.6)
    ctx.trigger_payload = {"entry_price": 100.0, "take_profit": 100.44}

    result = CodeEngine.run(ctx)

    assert result.recommended_sl_pct == pytest.approx(0.024)  # configured 2% x vol
    assert result.recommended_tp_pct == pytest.approx(
        0.0044
    )  # carried, no regime multiplier


def test_a_carried_level_on_the_wrong_side_of_the_entry_is_ignored():
    ctx = build_test_context(win_rate=0.6)
    ctx.trigger_payload = {
        "side": "BUY",
        "entry_price": 100.0,
        "stop_loss": 100.5,  # above a long's entry
        "take_profit": 99.0,  # below it
    }

    result = CodeEngine.run(ctx)

    assert result.recommended_sl_pct == pytest.approx(0.024)
    assert result.recommended_tp_pct == pytest.approx(0.032)


def test_the_entry_falls_back_to_the_current_price():
    ctx = build_test_context(win_rate=0.4)  # current price 50,000
    ctx.trigger_payload = {"stop_loss": 49890.0, "take_profit": 50110.0}

    result = CodeEngine.run(ctx)

    assert result.recommended_sl_pct == pytest.approx(0.0022)
    assert result.recommended_tp_pct == pytest.approx(0.0022)


def test_ev_and_kelly_are_derived_from_the_carried_22_basis_point_stop():
    win_rate, stop, target = 0.5, 0.0022, 0.0033
    ctx = build_test_context(win_rate=win_rate)
    ctx.trigger_payload = {
        "entry_price": 100.0,
        "stop_loss": 100.0 * (1 - stop),
        "take_profit": 100.0 * (1 + target),
    }

    result = CodeEngine.run(ctx)

    odds = target / stop
    assert result.gross_ev == pytest.approx(win_rate * target - (1 - win_rate) * stop)
    assert result.kelly_fraction == pytest.approx(win_rate - (1 - win_rate) / odds)
    # The configured 2% / 4% payoff would give a different Kelly fraction.
    assert result.kelly_fraction != pytest.approx(win_rate - (1 - win_rate) / 2.0)


def test_unavailable_strategy_defaults_skip_ev_and_kelly():
    ctx = build_test_context(win_rate=0.6)
    ctx.strategy_defaults = StrategyDefaults.unavailable()

    result = CodeEngine.run(ctx)

    assert result.ev_unavailable is True
    assert result.gross_ev is None
    assert result.kelly_position_usd is None


def test_the_unavailable_defaults_are_labelled_fallbacks():
    from cio.models.context import (
        FALLBACK_LEVERAGE,
        FALLBACK_MAX_HOLD_HOURS,
        FALLBACK_STOP_LOSS_PCT,
        FALLBACK_TAKE_PROFIT_PCT,
    )

    defaults = StrategyDefaults.unavailable()

    assert defaults.available is False
    assert (
        defaults.stop_loss_pct,
        defaults.take_profit_pct,
        defaults.leverage,
        defaults.max_hold_hours,
    ) == (
        FALLBACK_STOP_LOSS_PCT,
        FALLBACK_TAKE_PROFIT_PCT,
        FALLBACK_LEVERAGE,
        FALLBACK_MAX_HOLD_HOURS,
    )
    assert StrategyDefaults(
        stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
    ).available


def _levels_not_configured(**fields):
    """What a config of just {enabled: true} gives: every level is a labelled fallback."""
    from cio.models.context import (
        FALLBACK_LEVERAGE,
        FALLBACK_MAX_HOLD_HOURS,
        FALLBACK_STOP_LOSS_PCT,
        FALLBACK_TAKE_PROFIT_PCT,
    )

    return StrategyDefaults(
        stop_loss_pct=FALLBACK_STOP_LOSS_PCT,
        take_profit_pct=FALLBACK_TAKE_PROFIT_PCT,
        leverage=FALLBACK_LEVERAGE,
        max_hold_hours=FALLBACK_MAX_HOLD_HOURS,
        sl_configured=False,
        tp_configured=False,
        leverage_configured=False,
        max_hold_configured=False,
        **fields,
    )


def test_unconfigured_levels_without_carried_levels_give_no_ev_and_no_recommendation():
    ctx = build_test_context(win_rate=0.6)
    ctx.strategy_defaults = _levels_not_configured()

    result = CodeEngine.run(ctx)

    assert result.ev_unavailable is True
    assert result.gross_ev is None
    assert result.kelly_position_usd is None
    # The labelled fallback never reaches the recommended order parameters.
    assert result.recommended_sl_pct is None
    assert result.recommended_tp_pct is None
    assert result.leverage <= 1.0


def test_unconfigured_levels_with_both_levels_carried_compute_ev_from_them():
    ctx = build_test_context(win_rate=0.4)
    ctx.strategy_defaults = _levels_not_configured()
    ctx.trigger_payload = {
        "entry_price": 100.0,
        "stop_loss": 99.78,
        "take_profit": 100.44,
    }

    result = CodeEngine.run(ctx)

    assert result.ev_unavailable is False
    assert result.recommended_sl_pct == pytest.approx(0.0022)
    assert result.recommended_tp_pct == pytest.approx(0.0044)
    assert result.gross_ev == pytest.approx(0.4 * 0.0044 - 0.6 * 0.0022)


def test_unconfigured_levels_with_only_a_carried_stop_have_no_ev():
    ctx = build_test_context(win_rate=0.6)
    ctx.strategy_defaults = _levels_not_configured()
    ctx.trigger_payload = {"entry_price": 100.0, "stop_loss": 99.78}

    result = CodeEngine.run(ctx)

    assert result.ev_unavailable is True
    assert result.kelly_position_usd is None
    assert result.recommended_sl_pct == pytest.approx(0.0022)
    assert result.recommended_tp_pct is None


def test_a_configured_target_completes_a_carried_stop():
    ctx = build_test_context(win_rate=0.6)
    ctx.strategy_defaults = StrategyDefaults(
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        max_hold_hours=24,
        sl_configured=False,
        tp_configured=True,
    )
    ctx.trigger_payload = {"entry_price": 100.0, "stop_loss": 99.78}

    result = CodeEngine.run(ctx)

    assert result.ev_unavailable is False
    assert result.recommended_sl_pct == pytest.approx(0.0022)
    assert result.recommended_tp_pct == pytest.approx(0.032)  # configured 4% x regime


def test_a_parameter_change_on_an_unknown_level_is_skipped_not_a_crash():
    from cio.models import (
        CodeEngineResult,
        ParamChangeDirection,
        ParamChangeSignal,
    )

    code_result = CodeEngineResult(ev_unavailable=True)
    strategy_change = ParamChangeSignal(
        param="stop_loss_pct", direction=ParamChangeDirection.INCREASE, reason="test"
    )

    decision = _assemble(
        RiskLimits(max_position_size_usd=120.0, probe_mode=True),
        code_result,
        param_change=strategy_change,
    )

    assert decision.stop_loss_pct is None
    assert decision.computed_position_size_usd == pytest.approx(120.0)


def test_risk_limits_without_probe_mode_means_off():
    assert RiskLimits(max_position_size_usd=1000.0).probe_mode is False
    assert RiskLimits(**{"max_position_size_usd": 120.0, "probe_mode": True}).probe_mode


def _assemble(risk_limits, code_result, param_change=None):
    from cio.core.assembler import DecisionAssembler
    from cio.models import (
        ActionType,
        ActivationRecommendation,
        HealthStatus,
        RegimeFit,
        StrategyResult,
    )

    ctx = build_test_context()
    ctx.risk_limits = risk_limits
    strategy_result = StrategyResult(
        health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        regime_fit=RegimeFit.GOOD,
        param_change=param_change,
        thought_trace="test",
    )
    return DecisionAssembler.assemble(
        context=ctx,
        code_result=code_result,
        regime_result=ctx.regime,
        strategy_result=strategy_result,
        llm_action=ActionType.EXECUTE,
        llm_justification="test",
    )


def test_without_ev_the_decision_is_sized_at_the_probe_notional_in_probe_mode():
    from cio.models import CodeEngineResult

    code_result = CodeEngineResult(
        ev_unavailable=True, recommended_sl_pct=0.0022, recommended_tp_pct=0.0044
    )

    decision = _assemble(
        RiskLimits(max_position_size_usd=120.0, probe_mode=True), code_result
    )

    assert decision.computed_position_size_usd == pytest.approx(120.0)


def test_without_ev_and_without_probe_mode_the_old_fallback_size_stays():
    from cio.models import CodeEngineResult

    code_result = CodeEngineResult(
        ev_unavailable=True, recommended_sl_pct=0.02, recommended_tp_pct=0.04
    )

    decision = _assemble(RiskLimits(max_position_size_usd=1000.0), code_result)

    assert decision.computed_position_size_usd == pytest.approx(100.0)


def test_code_engine_uses_absolute_order_payoff_for_short_signal():
    ctx = build_test_context(win_rate=0.4)
    ctx.trigger_payload = {
        "side": "SELL",
        "entry_price": 100.0,
        "stop_loss": 100.22,
        "take_profit": 99.56,
    }

    result = CodeEngine.run(ctx)

    assert result.recommended_sl_pct == pytest.approx(0.0022)
    assert result.recommended_tp_pct == pytest.approx(0.0044)


def test_code_engine_suppresses_ev_for_empty_strategy_config():
    ctx = build_test_context(win_rate=0.6, portfolio_state_available=True)
    ctx.strategy_defaults = StrategyDefaults.unavailable()
    ctx.pre_decision_context.gaps.append(
        ContextGap(surface="strategy_defaults", reason="empty_config parameters={}")
    )

    result = CodeEngine.run(ctx)

    assert result.ev_unavailable is True
    assert result.gross_ev is None
    assert result.kelly_position_usd is None


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
