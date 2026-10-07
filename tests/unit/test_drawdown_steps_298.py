"""Drawdown steps at z sigma: reduce and halt new entries (petrosa-cio#298, rule 5)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.core.assembler import DecisionAssembler
from cio.core.context_builder import ContextBuilder
from cio.core.drawdown import basket_sigma, evaluate_drawdown, is_closing_intent
from cio.core.engine import CodeEngine
from cio.models import (
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    DrawdownState,
    HealthStatus,
    MarketSignals,
    PnlTrend,
    PortfolioSummary,
    RegimeEnum,
    RegimeFit,
    RegimeResult,
    RiskInputs,
    RiskLimits,
    StrategyDefaults,
    StrategyResult,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)

PAYLOAD = {
    "side": "BUY",
    "entry_price": 100.0,
    "stop_loss": 98.68,
    "take_profit": 101.98,
}
PAIRS = ["BTCUSDT", "ETHUSDT", "BCHUSDT", "LTCUSDT", "XRPUSDT"]


def _inputs(sigma=0.027, rho=1.0, pairs=PAIRS, equity_sigma=None, equity_ok=False):
    return RiskInputs(
        sigma_daily={s: sigma for s in pairs},
        correlation={a: {b: (1.0 if a == b else rho) for b in pairs} for a in pairs},
        equity_sigma=equity_sigma,
        equity_sufficient=equity_ok,
    )


def _dd(from_peak, net=0.42):
    return DrawdownState(from_peak=from_peak, net_notional_ratio=net, equity_now=1000.0)


def _context(
    *, drawdown=None, inputs=None, payload=None, probe_mode=False
) -> TriggerContext:
    return TriggerContext(
        correlation_id="cid",
        source_subject="test",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload=PAYLOAD if payload is None else payload,
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="t",
            thought_trace="t",
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="t",
            current_price=100.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="t",
        ),
        strategy_id="s1",
        strategy_stats=StrategyStats(
            win_rate=0.8, wins=160, losses=40, recent_pnl_trend=PnlTrend.NEUTRAL
        ),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=20_000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(
            max_drawdown_pct=0.1,
            max_orders_global=50,
            max_orders_per_symbol=5,
            max_position_size_usd=5000.0,
            probe_mode=probe_mode,
        ),
        drawdown_state=drawdown,
        risk_inputs=inputs,
    )


# --- the thresholds ------------------------------------------------------------------------------


def test_thresholds_match_the_1239_indicative_values():
    # net 0.42x equity, equal-weight basket sigma_1d 2.7%: model sigma 1.134%, reduce 2.3%, halt 3.4%
    decision = evaluate_drawdown(_dd(0.0), _inputs(sigma=0.027, rho=1.0))
    assert decision.basket_sigma == pytest.approx(0.027)
    assert decision.model_sigma == pytest.approx(0.42 * 0.027)
    assert decision.sigma_source == "model"
    assert decision.reduce_threshold == pytest.approx(0.0227, abs=0.0001)  # about 2.3%
    assert decision.halt_threshold == pytest.approx(0.0340, abs=0.0001)  # about 3.4%
    assert decision.threshold_source == "derived"
    assert (decision.z_reduce, decision.z_halt, decision.reduce_factor) == (
        2.0,
        3.0,
        0.5,
    )


def test_basket_sigma_uses_the_measured_correlation():
    two = ["AAA", "BBB"]
    assert basket_sigma(_inputs(sigma=0.02, rho=1.0, pairs=two))[0] == pytest.approx(
        0.02
    )
    # uncorrelated pairs diversify: sqrt(2 x 0.02^2) / 2
    assert basket_sigma(_inputs(sigma=0.02, rho=0.0, pairs=two))[0] == pytest.approx(
        (2 * 0.02**2) ** 0.5 / 2
    )


def test_the_larger_of_model_and_realized_sigma_wins():
    realized = evaluate_drawdown(
        _dd(0.0), _inputs(sigma=0.027, equity_sigma=0.02, equity_ok=True)
    )
    assert realized.sigma_source == "realized" and realized.sigma == 0.02
    assert realized.reduce_threshold == pytest.approx(0.04)
    assert realized.halt_threshold == pytest.approx(0.06)
    assert realized.model_sigma == pytest.approx(
        0.42 * 0.027
    )  # both components recorded

    model = evaluate_drawdown(
        _dd(0.0), _inputs(sigma=0.027, equity_sigma=0.005, equity_ok=True)
    )
    assert model.sigma_source == "model"


def test_a_net_short_book_uses_the_absolute_net_ratio():
    decision = evaluate_drawdown(_dd(0.0, net=-0.42), _inputs())
    assert decision.model_sigma == pytest.approx(0.42 * 0.027)


# --- the fallback --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inputs", "label"),
    [
        (None, "risk_inputs_unavailable"),
        (RiskInputs(), "model_sigma_no_pair_has_a_sufficient_daily_sigma"),
    ],
)
def test_without_usable_inputs_the_fixed_steps_apply_labelled(inputs, label):
    decision = evaluate_drawdown(_dd(0.0), inputs)
    assert decision.threshold_source == "fallback"
    assert decision.sigma_source == "fallback"
    assert (decision.reduce_threshold, decision.halt_threshold) == (0.03, 0.06)
    assert label in decision.fallbacks
    assert "thresholds_fallback" in decision.fallbacks


def test_an_insufficient_correlation_leaves_the_model_out():
    inputs = _inputs(pairs=["AAA", "BBB"])
    inputs.correlation["AAA"]["BBB"] = None
    decision = evaluate_drawdown(_dd(0.0), inputs)
    assert decision.model_sigma is None
    assert "model_sigma_no_sufficient_correlation_AAA_BBB" in decision.fallbacks
    assert decision.threshold_source == "fallback"


def test_only_a_sufficient_realized_sigma_still_derives_the_steps():
    inputs = RiskInputs(equity_sigma=0.01, equity_sufficient=True)
    decision = evaluate_drawdown(_dd(0.0), inputs)
    assert decision.sigma_source == "realized"
    assert decision.reduce_threshold == pytest.approx(0.02)
    assert "realized_sigma_insufficient" not in decision.fallbacks


def test_no_drawdown_block_is_not_evaluated():
    decision = evaluate_drawdown(None, _inputs())
    assert (
        decision.action == "not_evaluated" and decision.reason == "drawdown_unavailable"
    )
    assert (
        evaluate_drawdown(DrawdownState(from_peak=None), _inputs()).action
        == "not_evaluated"
    )


# --- the action ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("from_peak", "action"),
    [
        (0.0, "none"),
        (0.02, "none"),
        (0.0230, "reduce"),
        (0.0339, "reduce"),
        (0.0341, "halt"),
        (0.2, "halt"),
    ],
)
def test_the_action_follows_the_derived_steps(from_peak, action):
    assert evaluate_drawdown(_dd(from_peak), _inputs()).action == action


def test_the_fallback_steps_act_at_3_and_6_percent():
    assert evaluate_drawdown(_dd(0.029), None).action == "none"
    assert evaluate_drawdown(_dd(0.031), None).action == "reduce"
    assert evaluate_drawdown(_dd(0.061), None).action == "halt"


def test_closing_intents_are_recognised():
    assert is_closing_intent({"action": "close"})
    assert is_closing_intent({"side": "close_long"})
    assert is_closing_intent({"signal_type": "CLOSE_SHORT"})
    assert is_closing_intent({"side": "SELL", "reduce_only": True})
    assert not is_closing_intent({"side": "BUY"})
    assert not is_closing_intent({"action": "sell"})


# --- the engine ----------------------------------------------------------------------------------


def test_a_halt_blocks_new_entries_and_the_reason_says_why():
    result = CodeEngine.run(_context(drawdown=_dd(0.05), inputs=_inputs()))
    assert result.hard_blocked is True
    assert result.block_reason.startswith("drawdown_halt")
    assert "3.40%" in result.block_reason
    assert result.drawdown.action == "halt"


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "close", "entry_price": 100.0},
        {"side": "close_short"},
        {"side": "SELL", "reduce_only": True},
    ],
)
def test_closes_and_reduce_only_orders_pass_during_a_halt(payload):
    result = CodeEngine.run(
        _context(drawdown=_dd(0.05), inputs=_inputs(), payload=payload)
    )
    assert result.hard_blocked is False
    assert result.drawdown.action == "halt"  # still recorded


def test_a_reduce_halves_the_size_of_new_entries_but_never_below_the_probe():
    base = CodeEngine.run(_context(drawdown=_dd(0.0), inputs=_inputs()))
    reduced = CodeEngine.run(_context(drawdown=_dd(0.03), inputs=_inputs()))
    assert base.sizing.drawdown_factor == 1.0
    assert reduced.drawdown.action == "reduce"
    assert reduced.sizing.drawdown_factor == 0.5
    assert reduced.sizing.size_before_drawdown_usd == pytest.approx(
        base.sizing.final_size_usd
    )
    assert reduced.kelly_position_usd == pytest.approx(
        max(reduced.sizing.probe_usd, base.sizing.final_size_usd * 0.5)
    )
    probe = CodeEngine.run(
        _context(drawdown=_dd(0.03), inputs=_inputs(), probe_mode=True)
    )
    assert (
        probe.kelly_position_usd == probe.sizing.probe_usd
    )  # the smallest valid order is the floor


def test_a_reduce_does_not_shrink_a_close():
    closing = CodeEngine.run(
        _context(drawdown=_dd(0.03), inputs=_inputs(), payload={"action": "close"})
    )
    assert closing.sizing is None or closing.sizing.drawdown_factor == 1.0


def test_log_only_mode_computes_but_never_acts(monkeypatch):
    monkeypatch.setenv("CIO_DRAWDOWN_MODE", "log_only")
    halted = CodeEngine.run(_context(drawdown=_dd(0.05), inputs=_inputs()))
    assert halted.hard_blocked is False and halted.drawdown.action == "halt"
    reduced = CodeEngine.run(_context(drawdown=_dd(0.03), inputs=_inputs()))
    assert reduced.sizing.drawdown_factor == 1.0


def test_the_daily_realized_loss_stop_stays_a_separate_backstop():
    ctx = _context(drawdown=_dd(0.0), inputs=_inputs())
    ctx.global_drawdown_pct = 0.2  # over the 10% limit
    result = CodeEngine.run(ctx)
    assert result.hard_blocked is True
    assert result.block_reason.startswith("Global drawdown")


def test_the_decision_records_sigma_thresholds_drawdown_and_action():
    ctx = _context(
        drawdown=_dd(0.03), inputs=_inputs(equity_sigma=0.004, equity_ok=True)
    )
    code = CodeEngine.run(ctx)
    strategy = StrategyResult(
        strategy_id="s1",
        health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        regime_fit=RegimeFit.GOOD,
        confidence=1.0,
        thought_trace="t",
    )
    decision = DecisionAssembler.assemble(
        ctx,
        code,
        ctx.regime,
        strategy,
        llm_action=ActionType.EXECUTE,
        llm_justification="x",
    )
    record = decision.drawdown
    assert record.action == "reduce"
    assert record.drawdown == 0.03
    assert record.model_sigma == pytest.approx(0.42 * 0.027)
    assert record.realized_sigma == 0.004
    assert record.sigma_source == "model"
    assert (record.reduce_threshold, record.halt_threshold) == pytest.approx(
        (2 * 0.42 * 0.027, 3 * 0.42 * 0.027)
    )


def test_a_halted_decision_carries_the_record_too():
    ctx = _context(drawdown=_dd(0.05), inputs=_inputs())
    code = CodeEngine.run(ctx)
    strategy = StrategyResult(
        strategy_id="s1",
        health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        regime_fit=RegimeFit.GOOD,
        confidence=1.0,
        thought_trace="t",
    )
    decision = DecisionAssembler.assemble(ctx, code, ctx.regime, strategy)
    assert decision.action == ActionType.BLOCK
    assert decision.drawdown.action == "halt"


# --- the context builder -------------------------------------------------------------------------

# tradeengine's /state drawdown block (te#736): from_peak_pct is a FRACTION despite the name
TE_DRAWDOWN = {
    "equity_now": 4900.0,
    "equity_peak": 5000.0,
    "peak_at": "2026-10-06T12:00:00+00:00",
    "from_peak_pct": 0.02,
    "net_notional_ratio": 0.42,
    "as_of": "2026-10-07T01:00:00+00:00",
}


def _builder():
    b = ContextBuilder(
        data_manager_url="http://dm", tradeengine_url="http://te", clock=lambda: 0.0
    )
    b.client = MagicMock()
    return b


def test_te_drawdown_block_is_read_as_a_fraction():
    state = ContextBuilder._drawdown_from(TE_DRAWDOWN)
    assert state.from_peak == 0.02  # 2%, not 0.02%
    assert state.net_notional_ratio == 0.42
    assert evaluate_drawdown(state, None).drawdown == 0.02
    assert ContextBuilder._drawdown_from(None) is None
    assert ContextBuilder._drawdown_from({"from_peak_pct": "x"}) is None


@pytest.mark.asyncio
async def test_state_drawdown_block_reaches_the_context():
    payload = {
        "portfolio": {
            "gross_exposure": 0.2,
            "same_asset_pct": 0.1,
            "open_positions_count": 2,
        },
        "risk_limits": {
            "max_drawdown_pct": 0.1,
            "max_orders_global": 50,
            "max_orders_per_symbol": 5,
            "max_position_size_usd": 1000.0,
        },
        "env_stats": {"available_capital_usd": 5000.0},
        "drawdown": TE_DRAWDOWN,
    }
    builder = _builder()
    builder.client.get = AsyncMock(
        return_value=MagicMock(raise_for_status=lambda: None, json=lambda: payload)
    )
    _, _, env_stats = await builder._fetch_portfolio_and_risk("BTCUSDT", "cid")
    assert env_stats["drawdown"]["from_peak_pct"] == 0.02


RISK_INPUTS_BODY = {
    "symbols": {
        "BTCUSDT": {
            "sigma_daily_best": {
                "value": 0.02,
                "source": "klines_1d",
                "sufficient": True,
            }
        },
        "ETHUSDT": {
            "sigma_daily_best": {
                "value": 0.03,
                "source": "klines_1h",
                "sufficient": True,
            }
        },
        "BCHUSDT": {
            "sigma_daily_best": {"value": None, "source": None, "sufficient": False}
        },
    },
    "correlation": {
        "matrix": {
            "BTCUSDT": {"BTCUSDT": 1.0, "ETHUSDT": 0.8, "BCHUSDT": 0.5},
            "ETHUSDT": {"BTCUSDT": 0.8, "ETHUSDT": 1.0, "BCHUSDT": None},
        },
        "sufficient": {
            "BTCUSDT": {"BTCUSDT": True, "ETHUSDT": True, "BCHUSDT": False},
            "ETHUSDT": {"BTCUSDT": True, "ETHUSDT": True, "BCHUSDT": False},
        },
    },
    "equity": {"sigma_daily": 0.012, "sufficient": True, "peak": 5000.0},
}


@pytest.mark.asyncio
async def test_builder_keeps_only_sufficient_risk_inputs_and_caches():
    builder = _builder()
    builder.client.get = AsyncMock(
        return_value=MagicMock(
            raise_for_status=lambda: None, json=lambda: RISK_INPUTS_BODY
        )
    )
    inputs = await builder._fetch_risk_inputs("cid")
    assert inputs.sigma_daily == {
        "BTCUSDT": 0.02,
        "ETHUSDT": 0.03,
    }  # BCH is insufficient
    assert inputs.correlation["BTCUSDT"]["ETHUSDT"] == 0.8
    assert (
        inputs.correlation["BTCUSDT"]["BCHUSDT"] is None
    )  # insufficient correlation dropped
    assert (inputs.equity_sigma, inputs.equity_sufficient) == (0.012, True)
    await builder._fetch_risk_inputs("cid")
    assert builder.client.get.await_count == 1
    decision = evaluate_drawdown(_dd(0.0), inputs)
    assert decision.basket_symbols == ["BTCUSDT", "ETHUSDT"]
    # sqrt(0.02^2 + 0.03^2 + 2 x 0.8 x 0.02 x 0.03) / 2
    assert decision.basket_sigma == pytest.approx(
        (0.0004 + 0.0009 + 2 * 0.8 * 0.0006) ** 0.5 / 2
    )


@pytest.mark.asyncio
async def test_builder_gives_none_when_the_risk_inputs_cannot_be_read():
    builder = _builder()
    builder.client.get = AsyncMock(side_effect=RuntimeError("down"))
    assert await builder._fetch_risk_inputs("cid") is None
