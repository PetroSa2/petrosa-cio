"""Net-EV lower-confidence-bound gate and cost-share pre-filter (petrosa-cio#296, rules 1, 19, 23)."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.assembler import DecisionAssembler
from cio.core.context_builder import ContextBuilder
from cio.core.engine import CodeEngine
from cio.core.net_ev import (
    FALLBACK_MIN_NET_EV_R,
    beta_cdf,
    evaluate,
    gate_enforced,
    prefilter_mode,
)
from cio.core.orchestrator import Orchestrator
from cio.models import (
    ActionType,
    ActivationRecommendation,
    CodeEngineResult,
    CommissionRates,
    ConfidenceLevel,
    HealthStatus,
    MarketSignals,
    NetEvGate,
    PnlTrend,
    PortfolioSummary,
    RegimeEnum,
    RegimeFit,
    RegimeResult,
    RiskLimits,
    SlippageEstimate,
    StrategyDefaults,
    StrategyResult,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)

ENTRY = 100.0
PAYLOAD = {"side": "BUY", "entry_price": ENTRY, "stop_loss": 97.0, "take_profit": 104.0}


def _context(
    *,
    payload=None,
    win_rate=0.7,
    wins=70,
    losses=30,
    floor=6.0,  # tradeengine reports the floor in PERCENT
    floor_source="env",
    commission="exchange",
    slippage="measured",
    dm_regime="balanced_market",
    realized_pnl=None,
    budget=None,
    open_notional=0.0,
    total_notional=0.0,
) -> TriggerContext:
    if commission == "exchange":
        commission = CommissionRates(
            taker_rate=0.0004, maker_rate=0.0002, source="exchange"
        )
    if slippage == "measured":
        slippage = SlippageEstimate(regime=dm_regime or "x", median_bp=3.0, count=50)
    return TriggerContext(
        correlation_id="cid",
        source_subject="test",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload=PAYLOAD if payload is None else payload,
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="test",
            thought_trace="test",
            data_manager_regime=dm_regime,
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="t",
            current_price=ENTRY,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="t",
        ),
        strategy_id="s1",
        strategy_stats=StrategyStats(
            win_rate=win_rate,
            wins=wins,
            losses=losses,
            realized_pnl=realized_pnl,
            recent_pnl_trend=PnlTrend.NEUTRAL,
        ),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=10_000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(
            max_drawdown_pct=0.1,
            max_orders_global=50,
            max_orders_per_symbol=5,
            max_position_size_usd=1000.0,
            min_sl_distance_pct=floor,
            min_sl_distance_source=floor_source if floor else None,
        ),
        commission=commission,
        slippage=slippage,
        probation_budget_usd=budget,
        cold_start_open_notional_usd=open_notional,
        cold_start_total_notional_usd=total_notional,
    )


def _strategy() -> StrategyResult:
    return StrategyResult(
        strategy_id="s1",
        health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        regime_fit=RegimeFit.GOOD,
        confidence=1.0,
        thought_trace="t",
    )


def _assemble(ctx: TriggerContext, action: ActionType):
    code = CodeEngine.run(ctx)
    return DecisionAssembler.assemble(
        ctx, code, ctx.regime, _strategy(), llm_action=action, llm_justification="llm"
    ), code


# --- the Beta CDF ---------------------------------------------------------------------------------


@pytest.mark.parametrize("x", [0.1, 0.37, 0.5, 0.9])
def test_beta_cdf_matches_closed_forms(x):
    assert beta_cdf(x, 1.0, 1.0) == pytest.approx(x)
    assert beta_cdf(x, 3.0, 1.0) == pytest.approx(x**3)
    assert beta_cdf(x, 1.0, 4.0) == pytest.approx(1 - (1 - x) ** 4)
    assert beta_cdf(0.5, 7.5, 7.5) == pytest.approx(0.5)


def test_beta_cdf_edges_and_known_value():
    assert beta_cdf(0.0, 2.0, 3.0) == 0.0
    assert beta_cdf(1.0, 2.0, 3.0) == 1.0
    # I_0.3(2, 5) = 0.579825 (tabulated)
    assert beta_cdf(0.3, 2.0, 5.0) == pytest.approx(0.579825, abs=1e-6)


# --- the effective stop (rule 23) -----------------------------------------------------------------


def test_effective_stop_is_the_floor_and_take_profit_is_unchanged():
    gate = evaluate(_context(), 0.03, 0.04)
    assert gate.stop_pct == 0.03
    assert gate.stop_floor_frac == pytest.approx(0.06)
    assert gate.stop_floor_reported_pct == 6.0
    assert gate.s_eff == 0.06
    assert gate.take_profit_pct == 0.04
    # carried -3% / +4% looks 1.33:1, the effective -6% makes it 0.67:1
    assert gate.reward_risk == pytest.approx(2 / 3)
    c = 2 * 0.0004 + 2 * 3.0 / 10_000
    assert gate.cost_total == pytest.approx(c)
    assert gate.cost_share == pytest.approx(c / 0.06)
    assert gate.p_be == pytest.approx((0.06 + c) / (0.06 + 0.04))


def test_stop_wider_than_the_floor_is_kept():
    gate = evaluate(_context(floor=2.0), 0.03, 0.04)
    assert gate.s_eff == 0.03
    assert gate.reward_risk == pytest.approx(4 / 3)


def test_unknown_floor_uses_the_order_stop_and_is_labelled():
    gate = evaluate(_context(floor=None), 0.03, 0.04)
    assert gate.s_eff == 0.03
    assert "stop_floor_unavailable" in gate.fallbacks


# --- the posterior gate (rule 1) ------------------------------------------------------------------


def test_gate_passes_with_a_strong_posterior():
    gate = evaluate(_context(wins=140, losses=30, win_rate=140 / 170), 0.03, 0.04)
    assert gate.method == "posterior"
    assert gate.result == "pass"
    assert gate.prob_edge >= 0.8
    assert gate.alpha == 0.20
    assert gate.posterior.wins == 140 and gate.posterior.prior_strength == 30.0
    # the prior is centred on p_be: with no rounds P(win rate > p_be) would be one half
    assert gate.posterior.alpha == pytest.approx(30 * gate.p_be + 140)


def test_gate_fails_with_a_thin_posterior():
    gate = evaluate(_context(wins=6, losses=4, win_rate=0.6), 0.03, 0.04)
    assert gate.method == "posterior"
    assert gate.result == "fail"
    assert gate.prob_edge < 0.8
    # p_be is above the 0.50 target at the 6% floor: no n_req, so cold start cannot end
    assert gate.phase == "cold_start"
    assert gate.n_req is None
    assert gate.outcome == "veto"
    assert gate.reason == "net_ev_unreachable_payoff"


def test_gate_fails_when_costs_cannot_be_paid():
    # a 0.4% stop and target: round-trip cost above the whole target, p_be above 1
    gate = evaluate(_context(floor=None), 0.002, 0.002)
    assert gate.p_be > 1.0 or gate.result == "fail"
    assert gate.result == "fail"


def test_gate_not_evaluated_without_levels_or_win_rate():
    assert evaluate(_context(), None, 0.04).result == "not_evaluated"
    ctx = _context(win_rate=None, wins=None, losses=None)
    gate = evaluate(ctx, 0.03, 0.04)
    assert gate.result == "not_evaluated"
    assert gate.reason == "no_win_rate"


def test_alpha_is_an_operator_input(monkeypatch):
    monkeypatch.setenv("CIO_NET_EV_ALPHA", "0.5")
    assert evaluate(_context(), 0.03, 0.04).alpha == 0.5
    monkeypatch.setenv("CIO_NET_EV_ALPHA", "nonsense")
    assert evaluate(_context(), 0.03, 0.04).alpha == 0.20


# --- labelled fallbacks: one test per missing input ----------------------------------------------


def test_missing_commission_falls_back_to_5_bp_labelled():
    gate = evaluate(_context(commission=None), 0.03, 0.04)
    part = next(c for c in gate.costs if c.name == "commission")
    assert part.source == "fallback"
    assert part.value == pytest.approx(2 * 0.0005)
    assert "commission_fallback" in gate.fallbacks


def test_commission_reported_as_fallback_by_tradeengine_stays_labelled():
    reported = CommissionRates(taker_rate=0.0005, maker_rate=0.0002, source="fallback")
    gate = evaluate(_context(commission=reported), 0.03, 0.04)
    assert next(c for c in gate.costs if c.name == "commission").source == "fallback"


def test_measured_commission_is_labelled_exchange():
    gate = evaluate(_context(), 0.03, 0.04)
    assert next(c for c in gate.costs if c.name == "commission").source == "exchange"
    assert next(c for c in gate.costs if c.name == "slippage").source == "measured"


def test_missing_slippage_falls_back_and_doubles_on_turbulent_illiquidity():
    calm = evaluate(_context(slippage=None), 0.03, 0.04)
    turbulent = evaluate(
        _context(slippage=None, dm_regime="turbulent_illiquidity"), 0.03, 0.04
    )
    calm_part = next(c for c in calm.costs if c.name == "slippage")
    turb_part = next(c for c in turbulent.costs if c.name == "slippage")
    assert calm_part.source == turb_part.source == "fallback"
    assert turb_part.value == pytest.approx(2 * calm_part.value)
    assert "slippage_fallback" in calm.fallbacks


def test_too_few_slippage_fills_fall_back():
    few = SlippageEstimate(regime="balanced_market", median_bp=1.0, count=3)
    gate = evaluate(_context(slippage=few), 0.03, 0.04)
    assert next(c for c in gate.costs if c.name == "slippage").source == "fallback"


def test_favourable_measured_slippage_counts_as_zero():
    good = SlippageEstimate(regime="balanced_market", median_bp=-4.0, count=50)
    gate = evaluate(_context(slippage=good), 0.03, 0.04)
    assert next(c for c in gate.costs if c.name == "slippage").value == 0.0


def test_missing_posterior_falls_back_to_min_net_ev():
    # wins/losses unknown, point win rate 0.9: net EV = 0.9 * (R + 1) - 1 - c/S
    ctx = _context(wins=None, losses=None, win_rate=0.9)
    gate = evaluate(ctx, 0.03, 0.04)
    assert gate.method == "fallback_min_ev"
    assert "posterior_fallback" in gate.fallbacks
    assert gate.min_net_ev_r == FALLBACK_MIN_NET_EV_R
    assert gate.net_ev_r == pytest.approx(
        0.9 * (gate.reward_risk + 1) - 1 - gate.cost_share
    )
    assert gate.result == "pass"
    weak = evaluate(_context(wins=None, losses=None, win_rate=0.55), 0.03, 0.04)
    assert weak.method == "fallback_min_ev"
    assert weak.result == "fail"


# --- the deterministic post-LLM veto (rule 1) ----------------------------------------------------


def test_llm_execute_failing_the_gate_becomes_skip_with_the_reason():
    ctx = _context(wins=6, losses=4, win_rate=0.6)
    decision, _ = _assemble(ctx, ActionType.EXECUTE)
    assert decision.action == ActionType.SKIP
    assert "net_ev_unreachable_payoff" in decision.justification
    assert decision.cost_viable is False


def test_llm_execute_failing_the_enforced_gate_is_vetoed_with_lcb_reason():
    # past cold start (n >= n_req) the gate is enforced
    ctx = _context(floor=None, wins=40, losses=70, win_rate=40 / 110)
    decision, _ = _assemble(ctx, ActionType.EXECUTE)
    assert decision.net_ev_gate.phase == "enforced"
    assert decision.action == ActionType.SKIP
    assert "net_ev_lcb_below_zero" in decision.justification


def test_llm_modify_params_failing_the_gate_becomes_skip():
    decision, _ = _assemble(
        _context(wins=6, losses=4, win_rate=0.6), ActionType.MODIFY_PARAMS
    )
    assert decision.action == ActionType.SKIP
    assert "net_ev_unreachable_payoff" in decision.justification


def test_decision_record_carries_the_gate_inputs_and_result():
    decision, _ = _assemble(
        _context(wins=6, losses=4, win_rate=0.6), ActionType.EXECUTE
    )
    gate = decision.net_ev_gate
    assert gate.result == "fail"
    assert gate.p_be is not None and gate.prob_edge is not None and gate.alpha == 0.20
    assert {c.name: c.source for c in gate.costs} == {
        "commission": "exchange",
        "slippage": "measured",
    }
    assert gate.cost_share is not None


def test_llm_execute_passing_the_gate_is_kept():
    ctx = _context(wins=140, losses=30, win_rate=140 / 170)
    decision, code = _assemble(ctx, ActionType.EXECUTE)
    assert decision.action == ActionType.EXECUTE
    assert decision.net_ev_gate.result == "pass"
    # the size comes from the code and never above its computed size
    assert decision.computed_position_size_usd <= code.kelly_position_usd


def test_gate_never_upgrades_an_llm_skip():
    decision, _ = _assemble(_context(wins=140, losses=30), ActionType.SKIP)
    assert decision.action == ActionType.SKIP


def test_unevaluated_gate_does_not_veto_the_cold_start_path():
    ctx = _context(win_rate=None, wins=None, losses=None)
    decision, _ = _assemble(ctx, ActionType.EXECUTE)
    assert decision.action == ActionType.EXECUTE
    assert decision.net_ev_gate.result == "not_evaluated"


def test_log_only_mode_computes_but_never_vetoes(monkeypatch):
    monkeypatch.setenv("CIO_NET_EV_GATE_MODE", "log_only")
    assert gate_enforced() is False
    decision, _ = _assemble(
        _context(wins=6, losses=4, win_rate=0.6), ActionType.EXECUTE
    )
    assert decision.action == ActionType.EXECUTE
    assert decision.net_ev_gate.result == "fail"


def test_gate_is_evaluated_on_the_levels_after_a_parameter_change():
    ctx = _context(wins=140, losses=30, win_rate=140 / 170)
    code = CodeEngine.run(ctx)
    narrower = StrategyResult(
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
        narrower,
        llm_action=ActionType.EXECUTE,
        llm_justification="x",
    )
    assert decision.net_ev_gate.take_profit_pct == pytest.approx(
        decision.take_profit_pct
    )
    assert decision.net_ev_gate.stop_pct == pytest.approx(decision.stop_loss_pct)


# --- the cost-share pre-filter (rule 19) ---------------------------------------------------------


def test_cost_share_is_logged_on_every_engine_result():
    code = CodeEngine.run(_context())
    assert code.net_ev_gate is not None
    assert code.net_ev_gate.cost_share is not None


def test_cost_share_skip_when_cost_share_exceeds_the_limit():
    # a thin posterior around p_ref ~ 0.5 with a 1:1 payoff cannot pay a 1.2% round trip on a 2% stop
    ctx = _context(
        wins=5,
        losses=5,
        win_rate=0.5,
        floor=None,
        commission=CommissionRates(
            taker_rate=0.003, maker_rate=0.001, source="exchange"
        ),
    )
    gate = evaluate(ctx, 0.02, 0.02)
    assert gate.p_ref is not None
    assert gate.cost_share == pytest.approx(gate.cost_total / 0.02)
    assert gate.cost_share_limit == pytest.approx(gate.p_ref * 2 - 1)
    assert gate.cost_share_skip is True


def test_no_cost_share_skip_when_costs_are_small():
    gate = evaluate(_context(wins=140, losses=30), 0.03, 0.04)
    assert gate.cost_share_skip is False


def test_no_p_ref_without_a_posterior_means_no_prefilter():
    gate = evaluate(_context(wins=None, losses=None, win_rate=0.9), 0.03, 0.04)
    assert gate.p_ref is None
    assert gate.cost_share_skip is False


def test_prefilter_mode_defaults_to_log_only(monkeypatch):
    monkeypatch.delenv("CIO_COST_SHARE_PREFILTER", raising=False)
    assert prefilter_mode() == "log_only"
    monkeypatch.setenv("CIO_COST_SHARE_PREFILTER", "enforce")
    assert prefilter_mode() == "enforce"
    monkeypatch.setenv("CIO_COST_SHARE_PREFILTER", "off")
    assert prefilter_mode() == "off"
    monkeypatch.setenv("CIO_COST_SHARE_PREFILTER", "garbage")
    assert prefilter_mode() == "log_only"


# --- context builder: inputs from tradeengine and data-manager -----------------------------------


def _builder(clock=None) -> ContextBuilder:
    return ContextBuilder(
        data_manager_url="http://dm", tradeengine_url="http://te", clock=clock
    )


def test_commission_block_is_parsed_and_malformed_ones_are_ignored():
    raw = {
        "taker_rate": 0.0004,
        "maker_rate": 0.0002,
        "source": "exchange",
        "fee_burn": True,
    }
    assert ContextBuilder._commission_from(raw).taker_rate == 0.0004
    assert ContextBuilder._commission_from(None) is None
    assert ContextBuilder._commission_from({"taker_rate": "x"}) is None


@pytest.mark.asyncio
async def test_state_commission_and_stop_floor_reach_the_context():
    builder = _builder()
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
            "min_sl_distance_pct": 6.0,
            "min_sl_distance_source": "fallback",
        },
        "env_stats": {"available_capital_usd": 5000.0},
        "commission": {
            "taker_rate": 0.0004,
            "maker_rate": 0.0002,
            "source": "exchange",
        },
    }
    builder.client = MagicMock()
    builder.client.get = AsyncMock(
        return_value=MagicMock(raise_for_status=lambda: None, json=lambda: payload)
    )
    _, risk, env_stats = await builder._fetch_portfolio_and_risk("BTCUSDT", "cid")
    assert risk.min_sl_distance_pct == 6.0
    assert risk.min_sl_distance_frac == pytest.approx(0.06)
    assert risk.min_sl_distance_source == "fallback"
    assert env_stats["commission"]["taker_rate"] == 0.0004


@pytest.mark.asyncio
async def test_slippage_is_read_per_regime_cached_and_failures_give_none():
    builder = _builder(clock=lambda: 0.0)
    report = {
        "overall": {"count": 90, "median_bp": 2.0},
        "by_regime": {
            "balanced_market": {"count": 40, "median_bp": 1.5},
            "turbulent_illiquidity": {"count": 12, "median_bp": 4.0},
        },
    }
    builder.client = MagicMock()
    builder.client.get = AsyncMock(
        return_value=MagicMock(raise_for_status=lambda: None, json=lambda: report)
    )
    regime = RegimeResult(
        regime=RegimeEnum.RANGING,
        regime_confidence=ConfidenceLevel.HIGH,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="t",
        thought_trace="t",
        data_manager_regime="turbulent_illiquidity",
    )
    estimate = await builder._fetch_slippage(regime, "cid")
    assert (estimate.regime, estimate.median_bp, estimate.count) == (
        "turbulent_illiquidity",
        4.0,
        12,
    )
    assert (estimate.pooled_median_bp, estimate.pooled_count) == (2.0, 90)
    await builder._fetch_slippage(regime, "cid")
    assert builder.client.get.await_count == 1  # cached
    # a regime with no fills still carries the pooled median
    other = regime.model_copy(update={"data_manager_regime": "consolidation"})
    estimate = await builder._fetch_slippage(other, "cid")
    assert estimate.median_bp is None and estimate.count == 0
    assert estimate.pooled_count == 90
    unmapped = regime.model_copy(update={"data_manager_regime": None})
    assert await builder._fetch_slippage(unmapped, "cid") is None

    failing = _builder()
    failing.client = MagicMock()
    failing.client.get = AsyncMock(side_effect=RuntimeError("down"))
    assert await failing._fetch_slippage(regime, "cid") is None


# --- slippage fallback chain (operator ruling on #296) -------------------------------------------


def _slippage_part(slippage, dm_regime="balanced_market"):
    gate = evaluate(_context(slippage=slippage, dm_regime=dm_regime), 0.03, 0.04)
    return next(c for c in gate.costs if c.name == "slippage")


def test_slippage_chain_prefers_the_regime_median_with_enough_fills():
    part = _slippage_part(
        SlippageEstimate(
            regime="balanced_market",
            median_bp=3.0,
            count=10,
            pooled_median_bp=9.0,
            pooled_count=500,
        )
    )
    assert part.source == "measured"
    assert part.n == 10
    assert part.value == pytest.approx(2 * 3.0 / 10_000)


def test_slippage_chain_uses_the_pooled_median_when_the_regime_has_too_few_fills():
    part = _slippage_part(
        SlippageEstimate(
            regime="balanced_market",
            median_bp=3.0,
            count=9,
            pooled_median_bp=2.5,
            pooled_count=80,
        )
    )
    assert part.source == "measured_pooled"
    assert part.n == 80
    assert part.value == pytest.approx(2 * 2.5 / 10_000)


def test_slippage_chain_falls_back_when_neither_has_enough_fills():
    part = _slippage_part(
        SlippageEstimate(
            regime="turbulent_illiquidity",
            median_bp=3.0,
            count=9,
            pooled_median_bp=2.5,
            pooled_count=9,
        ),
        dm_regime="turbulent_illiquidity",
    )
    assert part.source == "fallback"
    # 2 bp per fill, doubled on turbulent_illiquidity, on entry and on exit
    assert part.value == pytest.approx(2 * 2.0 * 2.0 / 10_000)


# --- cold start (operator ruling on #296) ---------------------------------------------------------


def test_n_req_follows_rule_7():
    gate = evaluate(_context(floor=None, wins=6, losses=4, win_rate=0.6), 0.03, 0.04)
    delta = 0.5 - gate.p_be
    z = 0.8416212335729143  # inverse normal CDF at 1 - alpha, alpha = 0.20
    assert gate.n_req == pytest.approx(z * z * 0.25 / (delta * delta))
    assert gate.n == 10
    assert gate.target_win_rate == 0.5


def test_failing_cold_start_order_goes_at_probe_size_not_vetoed():
    ctx = _context(floor=None, wins=6, losses=4, win_rate=0.6)
    decision, _ = _assemble(ctx, ActionType.EXECUTE)
    gate = decision.net_ev_gate
    assert gate.result == "fail"
    assert (gate.phase, gate.outcome, gate.reason) == (
        "cold_start",
        "probe",
        "cold_start_probe",
    )
    assert decision.action == ActionType.EXECUTE
    assert decision.computed_position_size_usd == gate.cold_start.probe_notional_usd
    assert gate.cold_start.binding == "none"
    assert gate.cold_start.total_cap_usd == pytest.approx(1000.0)  # 10% of equity


def test_cold_start_probe_uses_the_tradeengine_probe_notional():
    ctx = _context(floor=None, wins=6, losses=4, win_rate=0.6)
    ctx.risk_limits.probe_mode = True
    ctx.risk_limits.max_position_size_usd = 7.5
    decision, _ = _assemble(ctx, ActionType.EXECUTE)
    assert decision.computed_position_size_usd == 7.5


def test_total_cold_start_notional_cap_binds_without_a_probation_budget():
    ctx = _context(floor=None, wins=6, losses=4, win_rate=0.6, total_notional=950.0)
    decision, _ = _assemble(ctx, ActionType.EXECUTE)
    gate = decision.net_ev_gate
    assert decision.action == ActionType.SKIP
    assert gate.outcome == "veto"
    assert gate.cold_start.binding == "total_notional_cap"
    assert gate.reason == "cold_start_limit_total_notional_cap"
    assert gate.cold_start.total_notional_usd == 950.0


def test_probation_budget_limits_are_loss_then_open_notional():
    base = {"floor": None, "wins": 6, "losses": 4, "win_rate": 0.6, "budget": 23.0}
    ok = evaluate(_context(**base), 0.03, 0.04)
    assert ok.outcome == "probe"
    assert ok.cold_start.open_notional_limit_usd == pytest.approx(23.0 / 0.03)
    assert ok.cold_start.budget_usd == 23.0

    lost = evaluate(_context(**base, realized_pnl=-30.0), 0.03, 0.04)
    assert lost.outcome == "veto"
    assert lost.cold_start.binding == "loss_budget"
    assert lost.cold_start.loss_so_far_usd == 30.0

    full = evaluate(_context(**base, open_notional=700.0), 0.03, 0.04)
    assert full.outcome == "veto"
    assert full.cold_start.binding == "open_notional"
    assert full.cold_start.open_notional_usd == 700.0
    # with a budget the total-notional fallback is not the limit
    assert full.cold_start.total_cap_usd is None


def test_cold_start_probe_is_not_applied_in_log_only_mode(monkeypatch):
    monkeypatch.setenv("CIO_NET_EV_GATE_MODE", "log_only")
    ctx = _context(floor=None, wins=6, losses=4, win_rate=0.6)
    decision, code = _assemble(ctx, ActionType.EXECUTE)
    assert decision.action == ActionType.EXECUTE
    assert decision.computed_position_size_usd == code.kelly_position_usd


@pytest.mark.asyncio
async def test_tracker_records_cold_start_notional_and_keeps_the_mark():
    from cio.core.portfolio_tracker import PortfolioTracker

    tracker = PortfolioTracker()
    await tracker.record_admit(
        strategy_id="a", position_size_usd=100.0, leverage=1.0, cold_start=True
    )
    await tracker.record_admit(
        strategy_id="b", position_size_usd=400.0, leverage=1.0, cold_start=False
    )
    assert await tracker.cold_start_notional("a") == (100.0, 100.0)
    assert await tracker.cold_start_notional("b") == (0.0, 100.0)
    # a later admission without the argument keeps the mark
    await tracker.record_admit(strategy_id="a", position_size_usd=60.0, leverage=1.0)
    assert await tracker.cold_start_notional("a") == (60.0, 60.0)
    await tracker.record_exit(strategy_id="a")
    assert await tracker.cold_start_notional("a") == (0.0, 0.0)


@pytest.mark.asyncio
async def test_orchestrator_marks_a_probe_decision_as_cold_start_notional():
    from cio.core.orchestrator import Orchestrator
    from cio.core.portfolio_tracker import PortfolioTracker

    tracker = PortfolioTracker()
    orchestrator = Orchestrator(portfolio_tracker=tracker)
    ctx = _context(floor=None, wins=6, losses=4, win_rate=0.6)
    decision, _ = _assemble(ctx, ActionType.EXECUTE)
    await orchestrator._record_cold_start(ctx, decision)
    size = decision.computed_position_size_usd
    assert await tracker.cold_start_notional("s1") == (size, size)
    skipped, _ = _assemble(ctx, ActionType.SKIP)
    other = PortfolioTracker()
    orchestrator.portfolio_tracker = other
    await orchestrator._record_cold_start(ctx, skipped)
    assert await other.cold_start_notional("s1") == (0.0, 0.0)


# --- contract with tradeengine's real /state shape (units) ---------------------------------------

# tradeengine's /state as it is today (te#741 stop floor, te#735 commission, te#736 drawdown): the floor
# is in PERCENT, its sibling min_sl_entry_distance_pct and the commission rates are fractions.
TE_STATE = {
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
        "min_sl_distance_pct": 6.0,
        "min_sl_distance_source": "fallback",
        "min_sl_entry_distance_pct": 0.005,
        "min_sl_entry_distance_source": "env",
    },
    "env_stats": {"available_capital_usd": 5000.0, "global_drawdown_pct": 0.012},
    "commission": {
        "taker_rate": 0.0005,
        "maker_rate": 0.0002,
        "source": "fallback",
        "fetched_at": None,
        "fee_burn": None,
    },
    "drawdown": {"equity": 5000.0, "from_peak_pct": 0.012},
}


async def _context_from_state(state, stop=0.03, tp=0.04):
    builder = _builder()
    builder.client = MagicMock()
    builder.client.get = AsyncMock(
        return_value=MagicMock(raise_for_status=lambda: None, json=lambda: state)
    )
    _, risk, env_stats = await builder._fetch_portfolio_and_risk("BTCUSDT", "cid")
    ctx = _context()
    ctx = ctx.model_copy(
        update={
            "risk_limits": risk,
            "commission": ContextBuilder._commission_from(env_stats.get("commission")),
        }
    )
    return ctx, evaluate(ctx, stop, tp)


@pytest.mark.asyncio
async def test_te_state_fallback_floor_in_percent_gives_s_eff_of_six_percent():
    ctx, gate = await _context_from_state(TE_STATE)
    assert ctx.risk_limits.min_sl_distance_frac == pytest.approx(0.06)
    assert gate.s_eff == pytest.approx(0.06)  # not 6.0
    assert gate.p_be < 1.0
    assert "stop_floor_fallback" in gate.fallbacks
    # commission rates are fractions: 5 bp taker, both sides
    part = next(c for c in gate.costs if c.name == "commission")
    assert part.value == pytest.approx(2 * 0.0005)
    assert part.source == "fallback"  # tradeengine labels it a fallback


@pytest.mark.asyncio
async def test_te_state_derived_floor_gives_the_derived_fraction():
    state = {
        **TE_STATE,
        "risk_limits": {
            **TE_STATE["risk_limits"],
            "min_sl_distance_pct": 1.32,
            "min_sl_distance_source": "derived",
        },
        "commission": {
            **TE_STATE["commission"],
            "taker_rate": 0.0004,
            "source": "exchange",
        },
    }
    # a ~22 bp carried stop is widened to the derived floor
    ctx, gate = await _context_from_state(state, stop=0.0022, tp=0.004)
    assert gate.s_eff == pytest.approx(0.0132)
    assert gate.stop_floor_reported_pct == 1.32
    assert gate.stop_floor_frac == pytest.approx(0.0132)
    assert "stop_floor_fallback" not in gate.fallbacks
    assert next(c for c in gate.costs if c.name == "commission").source == "exchange"


@pytest.mark.asyncio
async def test_te_state_env_source_is_accepted_and_a_wider_order_stop_wins():
    state = {
        **TE_STATE,
        "risk_limits": {**TE_STATE["risk_limits"], "min_sl_distance_source": "env"},
    }
    _, gate = await _context_from_state(state, stop=0.08)
    assert gate.s_eff == pytest.approx(0.08)
    assert gate.stop_floor_source == "env"


def test_the_entry_distance_sibling_is_not_read_as_the_stop_floor():
    risk = RiskLimits(**TE_STATE["risk_limits"])
    assert risk.min_sl_distance_frac == pytest.approx(0.06)  # not 0.005


def test_a_state_without_the_floor_leaves_it_unknown():
    risk = RiskLimits(max_orders_global=1, max_orders_per_symbol=1)
    assert risk.min_sl_distance_pct is None and risk.min_sl_distance_frac is None


# --- the cost-share pre-filter in the reasoning loop (rule 19): before any LLM call ------------------


def _code_result(skip: bool) -> CodeEngineResult:
    return CodeEngineResult(
        recommended_sl_pct=0.06,
        recommended_tp_pct=0.04,
        net_ev_gate=NetEvGate(
            result="fail",
            reason="net_ev_lcb_below_zero",
            method="posterior",
            s_eff=0.06,
            take_profit_pct=0.04,
            cost_total=0.03,
            cost_share=0.5,
            p_ref=0.5,
            cost_share_limit=-0.17,
            cost_share_skip=skip,
        ),
    )


async def _run(mode: str, skip: bool):
    env = {"NURSE_USE_LLM_REASONING": "true", "CIO_COST_SHARE_PREFILTER": mode}
    with (
        patch.dict(os.environ, env),
        patch("cio.core.orchestrator.CodeEngine") as engine,
        patch("cio.core.orchestrator.RegimeAnalyst") as regime,
        patch("cio.core.orchestrator.StrategyAssessor"),
        patch("cio.core.orchestrator.ActionClassifier"),
    ):
        engine.run.return_value = _code_result(skip)
        regime.return_value.classify = AsyncMock(side_effect=RuntimeError("stop here"))
        decision = await Orchestrator().run(_context())
        return decision, regime.return_value.classify


@pytest.mark.asyncio
async def test_enforce_skips_before_any_llm_call():
    decision, classify = await _run("enforce", skip=True)
    assert decision.action == ActionType.SKIP
    assert "cost_share_prefilter" in decision.justification
    classify.assert_not_called()


@pytest.mark.asyncio
async def test_log_only_by_default_does_not_skip():
    decision, classify = await _run("log_only", skip=True)
    classify.assert_called()  # the LLM stage still runs


@pytest.mark.asyncio
async def test_enforce_without_a_breach_proceeds():
    _, classify = await _run("enforce", skip=False)
    classify.assert_called()


@pytest.mark.asyncio
async def test_off_never_skips():
    _, classify = await _run("off", skip=True)
    classify.assert_called()
