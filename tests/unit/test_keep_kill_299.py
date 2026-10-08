"""Strategy keep/kill by posterior with an FDR budget and a probation budget (petrosa-cio#299)."""

import json
import logging
import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.core.context_builder import ContextBuilder
from cio.core.keep_kill import (
    StrategyScore,
    evaluate_strategies,
    keep_kill_mode,
    lower_bound,
    prob_positive_mean,
    probation_budget,
    t_cdf,
    t_quantile,
)
from cio.core.keep_kill_job import KeepKillJob, make_pause, parse_scorecard
from cio.models import ActionType


def _rounds(mean, sd, n):
    """n net-R values with exactly this mean and (sample) sd: alternating around the mean."""
    half = sd * math.sqrt((n - 1) / n)
    return [mean + (half if i % 2 == 0 else -half) for i in range(n)]


# --- the Student-t posterior ---------------------------------------------------------------------


def test_t_cdf_and_quantile_match_tabulated_values():
    assert t_cdf(0.0, 5) == 0.5
    assert t_cdf(2.228, 10) == pytest.approx(0.975, abs=1e-3)
    assert t_cdf(-2.228, 10) == pytest.approx(0.025, abs=1e-3)
    assert t_cdf(1.96, 10_000) == pytest.approx(0.975, abs=1e-3)  # close to the normal
    assert t_quantile(0.975, 10) == pytest.approx(2.228, abs=1e-3)
    assert t_quantile(0.8, 20) == pytest.approx(0.860, abs=1e-3)


def test_prob_positive_mean_and_the_lower_bound():
    sample = _rounds(0.3, 1.0, 30)
    p = prob_positive_mean(sample)
    assert p == pytest.approx(t_cdf(0.3 / (1.0 / math.sqrt(30)), 29))
    assert 0.9 < p < 1.0
    # the lower 80% bound is the mean minus t(0.8) x se
    assert lower_bound(sample, 0.8) == pytest.approx(
        0.3 - t_quantile(0.8, 29) / math.sqrt(30)
    )
    assert prob_positive_mean([1.0, 2.0]) is None
    assert (
        prob_positive_mean([0.5] * 5) == 1.0 and prob_positive_mean([-0.5] * 5) == 0.0
    )


def test_probation_budget_is_reduce_step_times_equity_over_strategies_on_probation():
    assert probation_budget(0.023, 1000.0, 1) == pytest.approx(23.0)
    assert probation_budget(0.03, 1000.0, 2) == pytest.approx(15.0)
    assert probation_budget(None, 1000.0, 2) is None
    assert probation_budget(0.03, 0.0, 2) is None
    assert probation_budget(0.03, 1000.0, 0) is None


# --- keep, kill, watch ---------------------------------------------------------------------------


def _score(sid, mean, sd=1.0, n=40, loss=None, closed=None):
    return StrategyScore(
        sid, _rounds(mean, sd, n), cumulative_net_loss_usd=loss, closed_rounds=closed
    )


def _by_id(records):
    return {r.strategy_id: r for r in records}


def test_the_fdr_adjustment_divides_the_budget_by_the_strategies_evaluated():
    one = evaluate_strategies([_score("a", 0.25)], reduce_step=0.03, equity_usd=1000.0)
    four = evaluate_strategies(
        [_score("a", 0.25), _score("b", 0.0), _score("c", 0.0), _score("d", 0.0)],
        reduce_step=0.03,
        equity_usd=1000.0,
    )
    assert one[0].alpha_adj == pytest.approx(0.20)
    assert four[0].alpha_adj == pytest.approx(0.05)
    assert four[0].strategies_evaluated == 4
    p = one[0].prob_net_ev_positive
    assert 0.8 < p < 0.95  # kept at alpha 0.20, not at alpha 0.05
    assert one[0].action == "keep"
    assert four[0].action == "watch"
    assert four[0].reason == "inside_the_fdr_band"


def test_kill_when_the_probation_budget_is_exhausted():
    scores = [
        _score("loser", 0.1, n=12, loss=30.0),  # on probation, lost $30
        _score("steady", 0.1, n=12, loss=2.0),  # on probation, small loss
        _score(
            "veteran", 0.2, n=60, loss=500.0
        ),  # not on probation: the budget does not apply
    ]
    records = _by_id(evaluate_strategies(scores, reduce_step=0.03, equity_usd=1000.0))
    # budget = 3% x 1000 / 2 strategies on probation = $15
    assert records["loser"].probation_budget_usd == pytest.approx(15.0)
    assert records["loser"].action == "kill"
    assert records["loser"].reason == "probation_budget_exhausted"
    assert records["loser"].probation_budget_source == "drawdown"
    assert records["steady"].action != "kill"
    assert (
        records["veteran"].action != "kill"
        and records["veteran"].probation_budget_usd is None
    )


def test_kill_when_p_net_ev_is_at_or_below_alpha_kill():
    scores = [_score("bad", -0.5, n=40), _score("good", 0.4, n=40)]
    records = _by_id(evaluate_strategies(scores, reduce_step=0.03, equity_usd=1000.0))
    assert records["bad"].prob_net_ev_positive <= records["bad"].alpha_kill
    assert records["bad"].action == "kill"
    assert records["bad"].reason == "p_net_ev_positive_at_or_below_alpha_kill"
    assert records["bad"].method == "derived"
    assert records["good"].action == "keep"


def test_the_record_carries_every_input_and_its_source():
    rec = evaluate_strategies(
        [_score("a", 0.3, n=16, loss=4.0)], reduce_step=0.03, equity_usd=2000.0
    )[0]
    assert (rec.n, rec.fdr_budget, rec.strategies_evaluated) == (16, 0.20, 1)
    assert rec.mean_net_r == pytest.approx(
        0.3
    ) and rec.cumulative_net_r == pytest.approx(4.8)
    assert rec.lower_bound_80 is not None and rec.prob_net_ev_positive is not None
    assert (rec.reduce_step, rec.equity_usd, rec.cumulative_net_loss_usd) == (
        0.03,
        2000.0,
        4.0,
    )
    assert rec.on_probation is True and rec.strategies_on_probation == 1
    assert rec.probation_budget_usd == pytest.approx(60.0)
    json.dumps(rec.as_dict())  # a logged record is plain data


def test_without_the_budget_inputs_a_probation_strategy_is_still_judged_and_labelled():
    rec = evaluate_strategies(
        [_score("a", 0.3, n=15, loss=999.0)], reduce_step=None, equity_usd=None
    )[0]
    assert rec.probation_budget_usd is None
    assert "probation_budget_unavailable" in rec.fallbacks
    assert rec.action != "kill"  # the posterior alone decides without a budget


# --- the labelled fallbacks ----------------------------------------------------------------------


def test_fallback_kills_at_minus_10_r_with_too_few_rounds_for_the_posterior():
    rec = evaluate_strategies(
        [StrategyScore("a", [-3.0, -4.0, -3.5])], reduce_step=0.03, equity_usd=1000.0
    )[0]
    assert rec.method == "fallback"
    assert rec.action == "kill" and rec.reason == "fallback_cumulative_below_minus_10r"
    assert any(f.startswith("fewer_than_") for f in rec.fallbacks)


def test_fallback_kills_when_the_lower_bound_is_below_minus_a_quarter_r():
    rec = evaluate_strategies(
        [StrategyScore("a", [-0.5, -0.4, -0.45, -0.5, -0.4])],
        reduce_step=0.03,
        equity_usd=1000.0,
    )[0]
    assert rec.method == "fallback"
    assert (
        rec.action == "kill" and rec.reason == "fallback_lower_bound_below_minus_0_25r"
    )


def test_fallback_watches_a_young_strategy_inside_the_band():
    rec = evaluate_strategies(
        [StrategyScore("a", [0.1, -0.1, 0.2, 0.0, 0.1])],
        reduce_step=0.03,
        equity_usd=1000.0,
    )[0]
    assert rec.method == "fallback" and rec.action == "watch"


def test_a_strategy_without_closed_rounds_is_not_evaluated():
    rec = evaluate_strategies(
        [StrategyScore("a", [])], reduce_step=0.03, equity_usd=1000.0
    )[0]
    assert rec.action == "not_evaluated" and rec.reason == "too_few_closed_rounds"


# --- the daily job -------------------------------------------------------------------------------

SCORECARD = {
    "strategies": {
        "bad": {
            "net_r": _rounds(-0.5, 1.0, 40),
            "cumulative_net_loss_usd": 5.0,
            "closed_rounds": 40,
        },
        "good": {
            "net_r": _rounds(0.4, 1.0, 40),
            "cumulative_net_loss_usd": 0.0,
            "closed_rounds": 40,
        },
        "young": {
            "net_r": [0.1, 0.2],
            "cumulative_net_loss_usd": 1.0,
            "closed_rounds": 2,
        },
    }
}


def _http(body=SCORECARD, fail=False):
    client = MagicMock()
    if fail:
        client.get = AsyncMock(side_effect=RuntimeError("down"))
    else:
        client.get = AsyncMock(
            return_value=MagicMock(raise_for_status=lambda: None, json=lambda: body)
        )
    return client


async def _risk():
    return 1000.0, 0.03


def test_parse_scorecard():
    scores = {s.strategy_id: s for s in parse_scorecard(SCORECARD)}
    assert scores["bad"].n == 40 and scores["young"].closed_rounds == 2
    assert parse_scorecard({}) == []
    assert parse_scorecard({"strategies": {"x": {"net_r": [None, 1.0]}}})[0].net_r == [
        1.0
    ]


@pytest.mark.asyncio
async def test_log_only_by_default_records_but_never_pauses(monkeypatch, caplog):
    monkeypatch.delenv("CIO_KEEP_KILL_MODE", raising=False)
    assert keep_kill_mode() == "log_only"
    pause = AsyncMock()
    job = KeepKillJob(_http(), "http://dm", _risk, pause=pause)
    with caplog.at_level(logging.INFO):
        records = await job.run_once()
    assert {r.strategy_id: r.action for r in records}["bad"] == "kill"
    pause.assert_not_awaited()
    lines = [r.message for r in caplog.records if r.message.startswith("KEEP_KILL ")]
    assert any("kill bad" in line for line in lines)
    assert any(
        '"strategy_id": "good"' in line for line in lines
    )  # a record per strategy


@pytest.mark.asyncio
async def test_enforce_pauses_a_killed_strategy_through_the_pause_path(monkeypatch):
    monkeypatch.setenv("CIO_KEEP_KILL_MODE", "enforce")
    pause = AsyncMock()
    job = KeepKillJob(_http(), "http://dm", _risk, pause=pause)
    await job.run_once()
    pause.assert_awaited_once()
    strategy_id, reason = pause.await_args.args
    assert strategy_id == "bad" and reason.startswith("keep_kill: ")


@pytest.mark.asyncio
async def test_a_missing_scorecard_gives_no_records_and_no_pause(monkeypatch):
    monkeypatch.setenv("CIO_KEEP_KILL_MODE", "enforce")
    pause = AsyncMock()
    job = KeepKillJob(_http(fail=True), "http://dm", _risk, pause=pause)
    assert await job.run_once() == []
    pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_missing_risk_snapshot_falls_back_without_stopping_the_job():
    async def broken():
        raise RuntimeError("no state")

    job = KeepKillJob(_http(), "http://dm", broken)
    records = await job.run_once()
    assert len(records) == 3 and all(r.reduce_step is None for r in records)


@pytest.mark.asyncio
async def test_the_probation_budget_is_served_for_the_cold_start_limit():
    job = KeepKillJob(_http(), "http://dm", _risk)
    await job.run_once()
    # young is the only strategy on probation (2 closed rounds): budget = 3% x 1000 / 1
    assert job.probation_budget("young") == pytest.approx(30.0)
    assert (
        job.probation_budget("good") is None and job.probation_budget("unknown") is None
    )


@pytest.mark.asyncio
async def test_make_pause_routes_a_pause_strategy_decision():
    builder = MagicMock()
    builder.build = AsyncMock(return_value="ctx")
    router = MagicMock()
    router.route = AsyncMock()
    await make_pause(builder, router)("bad", "keep_kill: test")
    builder.build.assert_awaited_once()
    context, decision = router.route.await_args.args
    assert context == "ctx"
    assert decision.action == ActionType.PAUSE_STRATEGY
    assert decision.justification == "keep_kill: test"


# --- the context builder -------------------------------------------------------------------------


def test_the_builder_serves_the_jobs_probation_budget_and_survives_its_failure():
    builder = ContextBuilder(data_manager_url="http://dm", tradeengine_url="http://te")
    assert builder._probation_budget("a") is None
    builder.probation_budget_provider = lambda sid: 12.5 if sid == "a" else None
    assert (
        builder._probation_budget("a") == 12.5
        and builder._probation_budget("b") is None
    )

    def boom(sid):
        raise RuntimeError("x")

    builder.probation_budget_provider = boom
    assert builder._probation_budget("a") is None


@pytest.mark.asyncio
async def test_risk_snapshot_gives_equity_and_the_reduce_step():
    builder = ContextBuilder(
        data_manager_url="http://dm", tradeengine_url="http://te", clock=lambda: 0.0
    )
    state = {
        "portfolio": {
            "gross_exposure": 0.2,
            "same_asset_pct": 0.1,
            "open_positions_count": 1,
        },
        "risk_limits": {
            "max_drawdown_pct": 0.1,
            "max_orders_global": 50,
            "max_orders_per_symbol": 5,
            "max_position_size_usd": 1000.0,
        },
        "env_stats": {"available_capital_usd": 4000.0, "equity": 5000.0},
        "drawdown": {"from_peak_pct": 0.0, "net_notional_ratio": 0.4},
    }

    async def get(url):
        body = {} if "risk/inputs" in url else state
        return MagicMock(raise_for_status=lambda: None, json=lambda: body)

    builder.client = MagicMock()
    builder.client.get = AsyncMock(side_effect=get)
    equity, reduce_step = await builder.risk_snapshot()
    assert equity == 5000.0
    assert reduce_step == 0.03  # no risk inputs: rule 5's labelled fallback


@pytest.mark.asyncio
async def test_the_loop_runs_once_then_waits_and_stops_at_once(monkeypatch):
    import asyncio

    http = _http()
    job = KeepKillJob(http, "http://dm", _risk, interval_seconds=3600.0)
    job.start()
    await asyncio.sleep(0.05)
    assert http.get.await_count == 1  # one run, then it waits for the next day
    await job.stop()  # wakes the wait immediately
    assert job._task.done()


@pytest.mark.asyncio
async def test_an_instant_sleep_cannot_turn_the_loop_into_a_busy_loop(monkeypatch):
    import asyncio

    http = _http()
    job = KeepKillJob(http, "http://dm", _risk, interval_seconds=3600.0)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())  # as the main() tests patch it
    job.start()
    await asyncio.sleep(0)  # the patched one: yields nothing
    for _ in range(20):
        await asyncio.get_running_loop().run_in_executor(None, lambda: None)
    assert http.get.await_count <= 1
    await job.stop()
