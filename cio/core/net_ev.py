"""Net-EV lower-confidence-bound gate and cost-share pre-filter (petrosa-cio#296).

Recorded rules 1, 19 and 23 on PetroSa2/petrosa_k8s#1239. Pure functions, no I/O.

Break-even win rate for a stop S (the stop that will really be placed), a take-profit T and a round-trip
cost c, all fractions of the entry: p_be = (S + c) / (S * (R + 1)) = (S + c) / (S + T) with R = T / S.
The order executes only if P(win rate > p_be) >= 1 - alpha under the strategy's posterior (a Beta prior
centred on p_be of strength k, plus the closed wins and losses). The cost share c / S is a cheap
pre-filter: skip when c / S > p_ref * (R + 1) - 1 with p_ref the shrunk win rate.
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from statistics import NormalDist
from typing import Literal

from cio.models.context import TriggerContext
from cio.models.net_ev import (
    ColdStartLimits,
    CommissionRates,
    CostComponent,
    IntegrityFlag,
    NetEvGate,
    NetEvPosterior,
    PriorStrength,
    SlippageEstimate,
    StrategyRounds,
)

#: Labelled fallbacks (each is logged with ``source: fallback``).
FALLBACK_TAKER_RATE = 0.0005
FALLBACK_MAKER_RATE = 0.0002
FALLBACK_SLIPPAGE_BP = 2.0  # per fill, before the turbulent multiplier
TURBULENT_SLIPPAGE_MULTIPLIER = (
    2.0  # decision 21: twice the slippage on turbulent_illiquidity
)
TURBULENT_REGIME = "turbulent_illiquidity"
FALLBACK_MIN_NET_EV_R = 0.10  # used when no posterior exists
#: Slippage statistics from fewer fills than this are not used.
MIN_SLIPPAGE_SAMPLES = 10

DEFAULT_ALPHA = 0.20
#: The holding time used when the strategy has no closed round to measure it from (the stop-floor H)
DEFAULT_HOLDING_FALLBACK_HOURS = 4.0
#: Rule 7 fallbacks: the time limit when the closed-round rate is unknown, and the integrity-flag factor
FALLBACK_COLD_START_DAYS = 14.0
INTEGRITY_HOLDING_FACTOR = 3.0
#: k is estimated across strategies once this many have at least MIN_ROUNDS_FOR_PRIOR closed rounds
MIN_STRATEGIES_FOR_PRIOR = 10
MIN_ROUNDS_FOR_PRIOR = 10
MAX_PRIOR_STRENGTH = 1000.0
DEFAULT_TARGET_WIN_RATE = (
    0.50  # rule 7: the win rate a cold-start strategy has to prove
)
#: Until the per-strategy probation budgets exist (petrosa-cio#299): total cold-start notional across
#: all strategies, as a fraction of equity (labelled fallback of rule 7).
FALLBACK_COLD_START_CAP_FRACTION = 0.10
DEFAULT_PRIOR_STRENGTH = 30.0

PrefilterMode = Literal["off", "log_only", "enforce"]


def net_ev_alpha() -> float:
    """alpha: the operator's confidence input (default 0.20, ``CIO_NET_EV_ALPHA``)."""
    return _env_float("CIO_NET_EV_ALPHA", DEFAULT_ALPHA, 0.0, 1.0)


def target_win_rate() -> float:
    """The target win rate of rule 7 (default 0.50, ``CIO_NET_EV_TARGET_WIN_RATE``)."""
    return _env_float("CIO_NET_EV_TARGET_WIN_RATE", DEFAULT_TARGET_WIN_RATE, 0.01, 0.99)


def prior_strength() -> float:
    """k: strength of the Beta prior until the sizing ticket supplies the estimated one."""
    return _env_float("CIO_NET_EV_PRIOR_STRENGTH", DEFAULT_PRIOR_STRENGTH, 0.0, 1e6)


def gate_enforced() -> bool:
    """False only when ``CIO_NET_EV_GATE_MODE=log_only``: the gate is computed and logged, never vetoes."""
    return os.getenv("CIO_NET_EV_GATE_MODE", "enforce").strip().lower() != "log_only"


def prefilter_mode() -> PrefilterMode:
    """The cost-share pre-filter: ``log_only`` (default), ``enforce`` or ``off`` (``CIO_COST_SHARE_PREFILTER``)."""
    value = os.getenv("CIO_COST_SHARE_PREFILTER", "log_only").strip().lower()
    return value if value in ("off", "enforce") else "log_only"  # type: ignore[return-value]


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    return value if low <= value <= high else default


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction of the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta function I_x(a, b): P(X <= x) for X ~ Beta(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def commission_components(
    commission: CommissionRates | None,
) -> tuple[CostComponent, list[str]]:
    """Round-trip commission: a taker fill on entry and on exit (a stop is a market order)."""
    if commission is not None and commission.source != "fallback":
        return (
            CostComponent(
                name="commission",
                value=2.0 * commission.taker_rate,
                source="exchange",
                detail=f"taker {commission.taker_rate:.6f} x2, fetched {commission.fetched_at}",
            ),
            [],
        )
    taker = commission.taker_rate if commission is not None else FALLBACK_TAKER_RATE
    reason = (
        "commission_fallback" if commission is None else "commission_fallback_reported"
    )
    return (
        CostComponent(
            name="commission",
            value=2.0 * taker,
            source="fallback",
            detail=f"taker {taker:.6f} x2 (5 bp taker / 2 bp maker fallback)",
        ),
        [reason],
    )


def slippage_component(
    slippage: SlippageEstimate | None, regime: str | None
) -> tuple[CostComponent, list[str]]:
    """Round-trip slippage, on entry and on exit, from the first step of the chain that has data.

    1. the measured per-fill median of the regime in force, once it has ``MIN_SLIPPAGE_SAMPLES`` fills;
    2. the pooled all-regime median, once it has that many;
    3. the labelled fallback, doubled on ``turbulent_illiquidity`` (decision 21).

    A median below zero (favourable) counts as zero. Each step records its source and n.
    """
    if (
        slippage is not None
        and slippage.source == "measured"
        and slippage.median_bp is not None
        and slippage.count >= MIN_SLIPPAGE_SAMPLES
    ):
        return (
            CostComponent(
                name="slippage",
                value=2.0 * max(0.0, slippage.median_bp) / 10_000.0,
                source="measured",
                n=slippage.count,
                detail=f"median {slippage.median_bp:.2f} bp x2, regime {slippage.regime}",
            ),
            [],
        )
    if (
        slippage is not None
        and slippage.pooled_median_bp is not None
        and slippage.pooled_count >= MIN_SLIPPAGE_SAMPLES
    ):
        return (
            CostComponent(
                name="slippage",
                value=2.0 * max(0.0, slippage.pooled_median_bp) / 10_000.0,
                source="measured_pooled",
                n=slippage.pooled_count,
                detail=(
                    f"pooled all-regime median {slippage.pooled_median_bp:.2f} bp x2 "
                    f"(regime {slippage.regime} has n={slippage.count})"
                ),
            ),
            [],
        )
    multiplier = TURBULENT_SLIPPAGE_MULTIPLIER if regime == TURBULENT_REGIME else 1.0
    per_fill_bp = FALLBACK_SLIPPAGE_BP * multiplier
    reason = (
        "slippage_fallback"
        if slippage is None
        else f"slippage_fallback_n={slippage.count}_pooled_n={slippage.pooled_count}"
    )
    return (
        CostComponent(
            name="slippage",
            value=2.0 * per_fill_bp / 10_000.0,
            source="fallback",
            n=0 if slippage is None else slippage.pooled_count,
            detail=f"{per_fill_bp:.2f} bp per fill x2, regime {regime or 'unknown'}",
        ),
        [reason],
    )


def _as_float(value: object) -> float | None:
    """A real number, else None (a missing or non-numeric level is an unknown level)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def evaluate(
    context: TriggerContext,
    stop_pct: float | None,
    take_profit_pct: float | None,
    now: datetime | None = None,
) -> NetEvGate:
    """Run the gate and the data-integrity check for the levels the order will carry."""
    now = now or datetime.now(UTC)
    gate = _evaluate(context, stop_pct, take_profit_pct, now)
    gate.integrity = integrity_flag(context, now)
    return gate


def _evaluate(
    context: TriggerContext,
    stop_pct: float | None,
    take_profit_pct: float | None,
    now: datetime,
) -> NetEvGate:
    """Run the gate for the stop and take-profit the order will carry.

    ``stop_pct`` is the carried or configured stop distance; the effective stop is the larger of it and
    tradeengine's floor (rule 23). The take-profit is unchanged by the floor.
    """
    limits = context.risk_limits
    alpha = net_ev_alpha()
    stop_pct, take_profit_pct = _as_float(stop_pct), _as_float(take_profit_pct)
    if (
        stop_pct is None
        or take_profit_pct is None
        or stop_pct <= 0
        or take_profit_pct <= 0
    ):
        return NetEvGate(
            result="not_evaluated",
            reason="levels_unknown",
            stop_pct=stop_pct,
            take_profit_pct=take_profit_pct,
            alpha=alpha,
        )

    fallbacks: list[str] = []
    floor = limits.min_sl_distance_frac
    if floor is None:
        fallbacks.append("stop_floor_unavailable")
    elif limits.min_sl_distance_source == "fallback":
        fallbacks.append("stop_floor_fallback")  # tradeengine's fixed placeholder
    s_eff = max(stop_pct, floor) if floor else stop_pct
    reward_risk = take_profit_pct / s_eff

    commission, commission_fallbacks = commission_components(context.commission)
    regime_key = context.regime.data_manager_regime
    slippage, slippage_fallbacks = slippage_component(context.slippage, regime_key)
    fallbacks += commission_fallbacks + slippage_fallbacks
    cost = commission.value + slippage.value

    p_be = (s_eff + cost) / (s_eff + take_profit_pct)
    gate = NetEvGate(
        result="not_evaluated",
        reason="",
        stop_pct=stop_pct,
        stop_floor_frac=floor,
        stop_floor_reported_pct=limits.min_sl_distance_pct,
        stop_floor_source=limits.min_sl_distance_source,
        s_eff=s_eff,
        take_profit_pct=take_profit_pct,
        reward_risk=reward_risk,
        costs=[commission, slippage],
        cost_total=cost,
        cost_share=cost / s_eff,
        p_be=p_be,
        alpha=alpha,
        fallbacks=fallbacks,
    )

    stats = context.strategy_stats
    wins, losses = stats.wins, stats.losses
    if wins is not None and losses is not None:
        gate.n = wins + losses
    if wins is not None and losses is not None and wins + losses > 0 and p_be < 1.0:
        prior = context.prior_strength
        k = prior.value if prior is not None else prior_strength()
        k_source = prior.source if prior is not None else "fallback"
        a = k * p_be + wins
        b = k * (1.0 - p_be) + losses
        mean = a / (a + b)
        gate.posterior = NetEvPosterior(
            wins=wins,
            losses=losses,
            prior_strength=k,
            k_source=k_source,
            alpha=a,
            beta=b,
            mean=mean,
        )
        gate.method = "posterior"
        gate.p_ref = mean
        gate.prob_edge = 1.0 - beta_cdf(p_be, a, b)
        gate.net_ev_r = mean * (reward_risk + 1.0) - 1.0 - gate.cost_share
        gate.result = "pass" if gate.prob_edge >= 1.0 - alpha else "fail"
        gate.reason = (
            "net_ev_lcb_ok" if gate.result == "pass" else "net_ev_lcb_below_zero"
        )
    elif p_be >= 1.0:
        gate.result = "fail"
        gate.reason = "net_ev_lcb_below_zero"
        gate.method = "posterior"
        gate.prob_edge = 0.0
        gate.net_ev_r = -1.0
    elif stats.win_rate is not None:
        # No posterior (the closed-round counts are missing): the labelled fallback, net EV >= +0.10R.
        gate.fallbacks.append("posterior_fallback")
        gate.method = "fallback_min_ev"
        gate.min_net_ev_r = FALLBACK_MIN_NET_EV_R
        gate.net_ev_r = stats.win_rate * (reward_risk + 1.0) - 1.0 - gate.cost_share
        gate.result = "pass" if gate.net_ev_r >= FALLBACK_MIN_NET_EV_R else "fail"
        gate.reason = (
            "net_ev_fallback_ok" if gate.result == "pass" else "net_ev_lcb_below_zero"
        )
    else:
        gate.reason = "no_win_rate"
        return gate

    if gate.p_ref is not None:
        gate.cost_share_limit = gate.p_ref * (reward_risk + 1.0) - 1.0
        gate.cost_share_skip = gate.cost_share > gate.cost_share_limit
    _decide_phase(context, gate, now)
    return gate


def required_rounds(p_be: float, alpha: float) -> float | None:
    """n_req = z^2 p (1 - p) / delta^2 with p the target win rate and delta = target - p_be (rule 7).

    None when delta <= 0: at the target win rate the setup can never prove an edge.
    """
    target = target_win_rate()
    delta = target - p_be
    if delta <= 0:
        return None
    z = NormalDist().inv_cdf(1.0 - min(max(alpha, 1e-6), 0.5))
    return z * z * target * (1.0 - target) / (delta * delta)


def _cold_start_time_is_up(
    rounds: StrategyRounds | None, gate: NetEvGate, now: datetime
) -> bool:
    """Rule 7's time limit: n_req / the observed closed-round rate, 14 days while the rate is unknown.

    A strategy that has been in cold start for longer than that leaves it even if it has not reached n_req
    (its closed rounds are accruing slower than the limit assumed). Without the strategy's first fill the
    limit cannot be applied and only n_req ends cold start.
    """
    if rounds is None or rounds.first_fill_at is None:
        return False
    started = rounds.first_fill_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    gate.days_in_cold_start = (now - started).total_seconds() / 86_400.0
    rate = rounds.closed_round_rate_per_day
    if gate.n_req is not None and rate is not None and rate > 0:
        gate.time_limit_days = gate.n_req / rate
        gate.time_limit_source = "observed_rate"
    else:
        gate.time_limit_days = FALLBACK_COLD_START_DAYS
        gate.time_limit_source = "fallback"
    return gate.days_in_cold_start >= gate.time_limit_days


def integrity_flag(context: TriggerContext, now: datetime) -> IntegrityFlag | None:
    """An open round older than 3x the strategy's median holding time: the exits are not being attributed.

    The median holding time comes from the round report; the labelled fallback is the stop-floor horizon
    (4 h). This is a data-integrity flag, not cold start: the order goes at probe size and carries a warning.
    """
    rounds = context.strategy_rounds
    if rounds is None or rounds.oldest_open_round_opened_at is None:
        return None
    opened = rounds.oldest_open_round_opened_at
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=UTC)
    age_h = (now - opened).total_seconds() / 3600.0
    if rounds.median_holding_seconds and rounds.median_holding_seconds > 0:
        median_h, source = rounds.median_holding_seconds / 3600.0, "median_holding_time"
    else:
        median_h, source = DEFAULT_HOLDING_FALLBACK_HOURS, "fallback"
    if age_h <= INTEGRITY_HOLDING_FACTOR * median_h:
        return None
    return IntegrityFlag(
        open_round_age_hours=age_h,
        median_holding_hours=median_h,
        holding_source=source,
    )


def estimate_prior_strength(
    strategies: list[tuple[int, int]],
) -> PriorStrength:
    """k of the Beta prior by the beta-binomial method of moments over per-strategy (wins, closed rounds).

    Needs at least ``MIN_STRATEGIES_FOR_PRIOR`` strategies with at least ``MIN_ROUNDS_FOR_PRIOR`` rounds each;
    until then the labelled fallback k = 30. With p_i = w_i / n_i, m the pooled rate and S = sum n_i (p_i - m)^2,
    the overdispersion rho = 1 / (k + 1) solves S / (m (1 - m)) = A + rho B with A = sum (1 - n_i / N) and
    B = sum (1 - n_i / N)(n_i - 1); k = 1 / rho - 1, kept within (1, MAX_PRIOR_STRENGTH]. No overdispersion
    (rho <= 0) means the strategies look alike: the strongest shrinkage.
    """
    usable = [(w, n) for w, n in strategies if n >= MIN_ROUNDS_FOR_PRIOR]
    if len(usable) < MIN_STRATEGIES_FOR_PRIOR:
        return PriorStrength(
            value=DEFAULT_PRIOR_STRENGTH,
            source="fallback",
            strategies_used=len(usable),
            reason=(
                f"{len(usable)} of {MIN_STRATEGIES_FOR_PRIOR} strategies have "
                f"{MIN_ROUNDS_FOR_PRIOR}+ closed rounds"
            ),
        )
    total_n = sum(n for _, n in usable)
    m = sum(w for w, _ in usable) / total_n
    if m <= 0.0 or m >= 1.0:
        return PriorStrength(
            value=DEFAULT_PRIOR_STRENGTH,
            source="fallback",
            strategies_used=len(usable),
            reason="pooled win rate is 0 or 1",
        )
    s = sum(n * (w / n - m) ** 2 for w, n in usable)
    a = sum(1.0 - n / total_n for _, n in usable)
    b = sum((1.0 - n / total_n) * (n - 1) for _, n in usable)
    rho = (s / (m * (1.0 - m)) - a) / b
    if rho <= 1.0 / (MAX_PRIOR_STRENGTH + 1.0):
        k = MAX_PRIOR_STRENGTH
    else:
        k = max(1.0, min(MAX_PRIOR_STRENGTH, 1.0 / rho - 1.0))
    return PriorStrength(value=k, source="estimated", strategies_used=len(usable))


def probe_notional(context: TriggerContext) -> float:
    """The probe size: tradeengine's smallest valid order when it sizes in probe mode, else the
    existing labelled fallback of the assembler (10% of the position cap, at most 500)."""
    limits = context.risk_limits
    if limits.probe_mode:
        return limits.max_position_size_usd
    return min(500.0, limits.max_position_size_usd * 0.1)


def _decide_phase(context: TriggerContext, gate: NetEvGate, now: datetime) -> None:
    """Rule 7 on the gate: enforced only after cold start; a failing cold-start order is sized at the
    probe notional while the limits hold, else vetoed (operator ruling on petrosa-cio#296)."""
    assert gate.p_be is not None and gate.alpha is not None
    gate.target_win_rate = target_win_rate()
    gate.n_req = required_rounds(gate.p_be, gate.alpha)
    if gate.n is None:
        gate.phase = (
            "enforced"  # no closed-round counts: the labelled fallback rule applies
        )
    elif gate.n_req is not None and gate.n >= gate.n_req:
        gate.phase = "enforced"
    elif _cold_start_time_is_up(context.strategy_rounds, gate, now):
        gate.phase = "enforced"
    else:
        gate.phase = "cold_start"

    if gate.result == "pass":
        gate.outcome = "pass"
        return
    if gate.phase == "enforced":
        gate.outcome = "veto"
        return  # reason stays net_ev_lcb_below_zero
    if gate.n_req is None:
        # p_be at or above the target win rate: delta <= 0, n_req is undefined
        gate.outcome = "veto"
        gate.reason = "net_ev_unreachable_payoff"
        return
    limits = _cold_start_limits(context, gate)
    gate.cold_start = limits
    if limits.binding == "none":
        gate.outcome = "probe"
        gate.reason = "cold_start_probe"
    else:
        gate.outcome = "veto"
        gate.reason = f"cold_start_limit_{limits.binding}"


def _cold_start_limits(context: TriggerContext, gate: NetEvGate) -> ColdStartLimits:
    """The probation limits on a cold-start strategy's probe-size order.

    With a per-strategy probation budget (a loss budget, petrosa-cio#299): (a) the strategy's
    cumulative net loss must not exceed it and (b) its open cold-start notional plus this probe must
    stay within budget / S_eff. Without one, the labelled fallback caps the total cold-start notional
    across all strategies at a fraction of equity.
    """
    probe = probe_notional(context)
    pnl = context.strategy_stats.realized_pnl
    loss = None if pnl is None else max(0.0, -pnl)
    budget = context.probation_budget_usd
    limits = ColdStartLimits(
        probe_notional_usd=probe,
        budget_usd=budget,
        budget_source="probation_budget" if budget is not None else "unavailable",
        loss_so_far_usd=loss,
        open_notional_usd=context.cold_start_open_notional_usd,
        total_notional_usd=context.cold_start_total_notional_usd,
    )
    if budget is not None:
        assert gate.s_eff is not None
        limits.open_notional_limit_usd = budget / gate.s_eff
        if loss is not None and loss > budget:
            limits.binding = "loss_budget"
        elif (
            context.cold_start_open_notional_usd + probe
            > limits.open_notional_limit_usd
        ):
            limits.binding = "open_notional"
        return limits
    limits.total_cap_usd = FALLBACK_COLD_START_CAP_FRACTION * max(
        0.0, context.available_capital_usd
    )
    if context.cold_start_total_notional_usd + probe > limits.total_cap_usd:
        limits.binding = "total_notional_cap"
    return limits


def log_gate(context: TriggerContext, gate: NetEvGate) -> None:
    """One structured line per evaluation: the gate record, with c/S on every decision."""
    import logging

    logging.getLogger(__name__).info(
        "NET_EV_GATE result=%s reason=%s method=%s s_eff=%s p_be=%s prob_edge=%s alpha=%s c=%s c_over_s=%s fallbacks=%s",
        gate.result,
        gate.reason,
        gate.method,
        _fmt(gate.s_eff),
        _fmt(gate.p_be),
        _fmt(gate.prob_edge),
        _fmt(gate.alpha),
        _fmt(gate.cost_total),
        _fmt(gate.cost_share),
        ",".join(gate.fallbacks) or "-",
        extra={
            "correlation_id": context.correlation_id,
            "strategy_id": context.strategy_id,
            "net_ev_gate": gate.model_dump(),
        },
    )


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.5f}"
