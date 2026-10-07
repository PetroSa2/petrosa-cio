"""Posterior sizing, estimated prior strength, cold-start exit and the integrity flag (petrosa-cio#297)."""

import random
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.core.assembler import DecisionAssembler
from cio.core.context_builder import ContextBuilder
from cio.core.engine import CodeEngine
from cio.core.net_ev import estimate_prior_strength, evaluate
from cio.core.sizing import kelly_fraction, size_order
from cio.models import (
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    HealthStatus,
    MarketSignals,
    PnlTrend,
    PortfolioSummary,
    PriorStrength,
    RegimeEnum,
    RegimeFit,
    RegimeResult,
    RiskLimits,
    StrategyDefaults,
    StrategyResult,
    StrategyRounds,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
# stop 1.32% (the derived floor), target 1.98%: R 1.5, p_be about 0.44 with the fallback costs
STOP, TP = 0.0132, 0.0198
PAYLOAD = {
    "side": "BUY",
    "entry_price": 100.0,
    "stop_loss": 98.68,
    "take_profit": 101.98,
}


def _context(
    *,
    wins=2,
    losses=0,
    equity=20_000.0,
    k=30.0,
    k_source="fallback",
    rounds=None,
    probe_mode=False,
    max_position=2000.0,
) -> TriggerContext:
    return TriggerContext(
        correlation_id="cid",
        source_subject="test",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload=PAYLOAD,
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="t",
            thought_trace="t",
            data_manager_regime="balanced_market",
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="t",
            current_price=100.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="t",
        ),
        strategy_id="iceberg_detector",
        strategy_stats=StrategyStats(
            win_rate=wins / (wins + losses) if wins + losses else None,
            wins=wins,
            losses=losses,
            recent_pnl_trend=PnlTrend.NEUTRAL,
        ),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=equity,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(
            max_drawdown_pct=0.1,
            max_orders_global=50,
            max_orders_per_symbol=5,
            max_position_size_usd=max_position,
            probe_mode=probe_mode,
        ),
        prior_strength=PriorStrength(value=k, source=k_source),
        strategy_rounds=rounds,
    )


def _size(ctx):
    gate = evaluate(ctx, STOP, TP, now=NOW)
    return gate, size_order(ctx, gate)


# --- the prior strength k estimated across strategies --------------------------------------------


def _strategies(k, count, rounds, seed=1, mean=0.5):
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        p = rng.betavariate(mean * k, (1 - mean) * k)
        wins = sum(rng.random() < p for _ in range(rounds))
        out.append((wins, rounds))
    return out


def test_k_is_estimated_by_method_of_moments():
    # strategies drawn from Beta(10, 10): k = 20
    estimate = estimate_prior_strength(_strategies(20, 300, 200))
    assert estimate.source == "estimated"
    assert estimate.strategies_used == 300
    assert 14.0 <= estimate.value <= 28.0


def test_more_alike_strategies_give_a_stronger_prior():
    spread = estimate_prior_strength(_strategies(8, 300, 200)).value
    alike = estimate_prior_strength(_strategies(80, 300, 200)).value
    assert spread < alike


def test_k_falls_back_to_30_until_ten_strategies_have_ten_rounds():
    nine = [(5, 10)] * 9
    est = estimate_prior_strength(nine)
    assert (est.value, est.source) == (30.0, "fallback")
    assert est.strategies_used == 9
    # ten strategies, but one has too few rounds
    assert estimate_prior_strength([(5, 10)] * 9 + [(2, 9)]).source == "fallback"
    ten = [(w, 20) for w in (4, 6, 8, 10, 12, 14, 16, 5, 11, 9)]
    assert estimate_prior_strength(ten).source == "estimated"


def test_pooled_rate_of_zero_or_one_keeps_the_fallback():
    assert estimate_prior_strength([(0, 20)] * 12).source == "fallback"
    assert estimate_prior_strength([(20, 20)] * 12).source == "fallback"


def test_the_gate_uses_the_context_k_and_records_its_source():
    gate30 = evaluate(_context(k=30.0, k_source="fallback"), STOP, TP, now=NOW)
    gate12 = evaluate(_context(k=12.0, k_source="estimated"), STOP, TP, now=NOW)
    assert (gate30.posterior.prior_strength, gate30.posterior.k_source) == (
        30.0,
        "fallback",
    )
    assert (gate12.posterior.prior_strength, gate12.posterior.k_source) == (
        12.0,
        "estimated",
    )
    assert (
        gate12.posterior.mean > gate30.posterior.mean
    )  # a weaker prior moves toward 2/2 faster


# --- continuous sizing (rule 2) ------------------------------------------------------------------


def test_kelly_fraction():
    assert kelly_fraction(0.6, 1.0) == pytest.approx(0.2)
    assert kelly_fraction(0.4, 1.0) == 0.0
    assert kelly_fraction(0.9, 0.0) == 0.0


def test_iceberg_detector_at_two_of_two_with_k_30_sizes_at_about_the_probe():
    gate, sizing = _size(_context(wins=2, losses=0, k=30.0))
    assert gate.method == "posterior"
    assert sizing.probe_usd == pytest.approx(
        200.0
    )  # no probe mode: 10% of the 2000 position cap
    # the formula lands within a fifth of the probe: the posterior barely moved from the prior
    assert sizing.final_size_usd == pytest.approx(sizing.probe_usd, rel=0.2)
    assert sizing.k == 30.0 and sizing.k_source == "fallback"
    assert sizing.p_post == pytest.approx(gate.posterior.mean)
    assert sizing.prob_net_ev_positive == pytest.approx(gate.prob_edge)


def test_a_weaker_prior_sizes_more_and_no_tiers_exist_in_between():
    sizes = {}
    for k in (30.0, 20.0, 12.0, 6.0):
        _, sizing = _size(_context(wins=2, losses=0, k=k, k_source="estimated"))
        sizes[k] = sizing.final_size_usd
    assert sizes[30.0] <= sizes[20.0] <= sizes[12.0] <= sizes[6.0]
    assert sizes[12.0] > 2 * sizes[30.0]  # k = 12 is far above the probe
    # continuous: a different k gives a different number, not a tier
    assert len({round(v, 2) for v in sizes.values()}) > 2


def test_size_is_the_formula_when_above_the_probe():
    ctx = _context(wins=2, losses=0, k=12.0, k_source="estimated", equity=20_000.0)
    gate, sizing = _size(ctx)
    expected = sizing.f_q * sizing.kelly_fraction * 20_000.0 * gate.prob_edge
    assert sizing.kelly_size_usd == pytest.approx(expected)
    assert sizing.final_size_usd == pytest.approx(expected)
    assert sizing.binding == "kelly"
    b_net = (TP - gate.cost_total) / (gate.s_eff + gate.cost_total)
    assert sizing.b_net == pytest.approx(b_net)
    assert sizing.kelly_fraction == pytest.approx(
        kelly_fraction(gate.posterior.mean, b_net)
    )


def test_size_is_capped_by_the_position_limit():
    ctx = _context(wins=180, losses=20, k=30.0, equity=1_000_000.0, max_position=2000.0)
    _, sizing = _size(ctx)
    assert sizing.binding == "max_position"
    assert sizing.final_size_usd == 2000.0
    assert sizing.kelly_size_usd > 2000.0


def test_probe_mode_sizes_at_tradeengines_probe_notional():
    ctx = _context(wins=180, losses=20, probe_mode=True, max_position=7.5)
    _, sizing = _size(ctx)
    assert sizing.probe_usd == 7.5
    assert sizing.final_size_usd == 7.5


def test_no_posterior_sizes_at_the_probe_with_the_reason():
    ctx = _context(wins=0, losses=0)
    ctx.strategy_stats.win_rate = 0.6  # a win rate without counts
    _, sizing = _size(ctx)
    assert sizing.reason == "no_posterior"
    assert sizing.final_size_usd == sizing.probe_usd


def test_the_engine_sizes_from_the_posterior_and_the_decision_records_it():
    ctx = _context(wins=2, losses=0, k=12.0, k_source="estimated")
    code = CodeEngine.run(ctx)
    assert code.sizing is not None
    assert code.kelly_position_usd == code.sizing.final_size_usd
    strategy = StrategyResult(
        strategy_id="iceberg_detector",
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
    record = decision.sizing
    assert record is not None
    assert (record.k, record.k_source) == (12.0, "estimated")
    assert record.p_post is not None and record.prob_net_ev_positive is not None
    assert record.kelly_fraction is not None
    assert decision.computed_position_size_usd == record.final_size_usd


# --- cold-start exit by time (rule 7) ------------------------------------------------------------


def _rounds(**kwargs):
    return StrategyRounds(**kwargs)


def test_n_req_and_the_time_limit_from_the_observed_rate():
    rounds = _rounds(
        first_fill_at=NOW - timedelta(days=20), closed_round_rate_per_day=0.5
    )
    gate = evaluate(_context(wins=6, losses=4, rounds=rounds), STOP, TP, now=NOW)
    assert gate.n_req is not None
    assert gate.time_limit_source == "observed_rate"
    assert gate.time_limit_days == pytest.approx(gate.n_req / 0.5)
    assert gate.days_in_cold_start == pytest.approx(20.0)
    assert gate.phase == "cold_start"  # 20 days is within n_req / 0.5


def test_cold_start_ends_when_the_time_limit_is_up():
    rounds = _rounds(
        first_fill_at=NOW - timedelta(days=20), closed_round_rate_per_day=5.0
    )
    gate = evaluate(_context(wins=6, losses=4, rounds=rounds), STOP, TP, now=NOW)
    assert gate.time_limit_days == pytest.approx(gate.n_req / 5.0)
    assert gate.time_limit_days < 20
    assert gate.phase == "enforced"
    assert (
        gate.outcome == "veto"
    )  # the failing order is no longer given the cold-start allowance


def test_the_time_limit_falls_back_to_14_days_without_a_rate():
    young = _rounds(first_fill_at=NOW - timedelta(days=5))
    gate = evaluate(_context(wins=6, losses=4, rounds=young), STOP, TP, now=NOW)
    assert (gate.time_limit_days, gate.time_limit_source) == (14.0, "fallback")
    assert gate.phase == "cold_start"
    old = _rounds(first_fill_at=NOW - timedelta(days=15))
    assert (
        evaluate(_context(wins=6, losses=4, rounds=old), STOP, TP, now=NOW).phase
        == "enforced"
    )


def test_without_round_statistics_only_n_req_ends_cold_start():
    gate = evaluate(_context(wins=6, losses=4, rounds=None), STOP, TP, now=NOW)
    assert gate.phase == "cold_start" and gate.time_limit_days is None


# --- the data-integrity flag ---------------------------------------------------------------------


def test_an_open_round_older_than_three_median_holding_times_raises_the_flag():
    rounds = _rounds(
        fills=40,
        oldest_open_round_opened_at=NOW - timedelta(hours=30),
        median_holding_seconds=4 * 3600,
    )
    gate = evaluate(_context(rounds=rounds), STOP, TP, now=NOW)
    flag = gate.integrity
    assert flag is not None
    assert flag.reason == "no_closed_round_within_3x_median_holding"
    assert flag.open_round_age_hours == pytest.approx(30.0)
    assert (flag.median_holding_hours, flag.holding_source) == (
        4.0,
        "median_holding_time",
    )


def test_a_younger_open_round_raises_no_flag():
    rounds = _rounds(
        oldest_open_round_opened_at=NOW - timedelta(hours=10),
        median_holding_seconds=4 * 3600,
    )
    assert evaluate(_context(rounds=rounds), STOP, TP, now=NOW).integrity is None
    assert evaluate(_context(rounds=None), STOP, TP, now=NOW).integrity is None
    assert evaluate(_context(rounds=_rounds()), STOP, TP, now=NOW).integrity is None


def test_bollinger_squeeze_alert_225_fills_no_rounds_is_flagged_with_the_labelled_holding():
    rounds = _rounds(
        fills=225,
        closed_rounds=0,
        open_rounds=1,
        oldest_open_round_opened_at=NOW - timedelta(days=3),
    )
    gate = evaluate(_context(wins=0, losses=0, rounds=rounds), STOP, TP, now=NOW)
    assert gate.integrity is not None
    assert (gate.integrity.median_holding_hours, gate.integrity.holding_source) == (
        4.0,
        "fallback",
    )


def test_the_flag_sizes_at_the_probe_with_a_warning_and_is_not_cold_start():
    rounds = _rounds(
        oldest_open_round_opened_at=NOW - timedelta(days=3),
        median_holding_seconds=4 * 3600,
    )
    ctx = _context(wins=180, losses=20, equity=1_000_000.0, rounds=rounds)
    gate, sizing = _size(ctx)
    assert gate.integrity is not None
    assert sizing.reason == "data_integrity_flag"
    assert sizing.final_size_usd == sizing.probe_usd

    code = CodeEngine.run(ctx)
    assert any(w.startswith("DATA_INTEGRITY") for w in code.risk_warnings)
    assert code.kelly_position_usd == code.sizing.probe_usd


# --- the context builder reads the round report --------------------------------------------------


def _builder():
    b = ContextBuilder(
        data_manager_url="http://dm", tradeengine_url="http://te", clock=lambda: 0.0
    )
    b.client = MagicMock()
    return b


def _report(strategies):
    return {"strategies": strategies}


def _stats(wins, losses, **extra):
    base = {
        "fills": wins + losses,
        "closed_rounds": wins + losses,
        "open_rounds": 0,
        "wins": wins,
        "losses": losses,
        "closed_round_rate_per_day": 1.5,
        "median_holding_seconds": 7200.0,
        "first_fill_at": "2026-09-20T00:00:00+00:00",
        "last_closed_at": "2026-10-06T00:00:00+00:00",
        "oldest_open_round_opened_at": None,
    }
    base.update(extra)
    return base


@pytest.mark.asyncio
async def test_builder_reads_the_round_report_and_estimates_k_when_enough_strategies():
    strategies = {
        f"s{i}": _stats(w, 20 - w)
        for i, w in enumerate((4, 6, 8, 10, 12, 14, 16, 5, 11, 9, 7))
    }
    strategies["iceberg_detector"] = _stats(2, 0)
    builder = _builder()
    builder.client.get = AsyncMock(
        return_value=MagicMock(
            raise_for_status=lambda: None, json=lambda: _report(strategies)
        )
    )
    prior, rounds = await builder._fetch_rounds("iceberg_detector", "cid")
    assert prior.source == "estimated" and prior.strategies_used == 11
    assert (rounds.wins, rounds.losses) == (2, 0)
    assert rounds.median_holding_seconds == 7200.0
    assert rounds.first_fill_at.year == 2026
    await builder._fetch_rounds("s1", "cid")
    assert builder.client.get.await_count == 1  # cached
    prior, rounds = await builder._fetch_rounds("unknown_strategy", "cid")
    assert rounds is None and prior is not None


@pytest.mark.asyncio
async def test_builder_uses_the_fallback_k_with_few_strategies_and_none_on_failure():
    builder = _builder()
    builder.client.get = AsyncMock(
        return_value=MagicMock(
            raise_for_status=lambda: None, json=lambda: _report({"a": _stats(5, 5)})
        )
    )
    prior, _ = await builder._fetch_rounds("a", "cid")
    assert (prior.value, prior.source) == (30.0, "fallback")

    failing = _builder()
    failing.client.get = AsyncMock(side_effect=RuntimeError("down"))
    assert await failing._fetch_rounds("a", "cid") == (None, None)
