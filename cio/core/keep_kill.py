"""Strategy keep/kill by posterior with an FDR budget and a probation loss budget (petrosa-cio#299, rule 10).

Judged net of costs, in R-multiples, from the strategy's closed rounds (data-manager's net-of-cost scorecard,
petrosa-data-manager#468). Pure, no I/O.

* **P(net EV > 0)** is the posterior probability that the mean net R is positive (Student-t on the n closed
  rounds).
* **Keep** when P(net EV > 0) >= 1 - alpha_adj, alpha_adj = FDR budget / the number of strategies evaluated.
* **Kill** (pause) when the strategy's cumulative net loss on probation reaches the **probation budget** (a
  loss budget in USD: drawdown reduce-step x equity / the number of strategies on probation) or when
  P(net EV > 0) <= alpha_kill.
* **Fallbacks** (labelled), when the derived inputs are missing: keep at n >= 100 with the lower 80% bound of the
  mean net R above 0; kill at a cumulative -10R or a lower bound below -0.25R.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from statistics import mean, stdev
from typing import Literal

from cio.core.net_ev import beta_cdf

DEFAULT_FDR_BUDGET = 0.20  # operator input (pending confirmation)
FALLBACK_KEEP_MIN_ROUNDS = 100
FALLBACK_KILL_CUMULATIVE_R = -10.0
FALLBACK_KILL_LOWER_BOUND_R = -0.25
FALLBACK_LOWER_BOUND_CONFIDENCE = 0.80
#: A strategy with fewer closed rounds than this is on probation (rule 7's labelled fallback).
PROBATION_MIN_ROUNDS = 30
MIN_ROUNDS_TO_JUDGE = 3
#: The Student-t posterior is used from this many closed rounds; below it the labelled fallbacks apply.
DERIVED_MIN_ROUNDS = 10


def fdr_budget() -> float:
    try:
        value = float(os.environ["CIO_FDR_BUDGET"])
    except (KeyError, ValueError):
        return DEFAULT_FDR_BUDGET
    return value if 0.0 < value < 1.0 else DEFAULT_FDR_BUDGET


def keep_kill_mode() -> str:
    """``log_only`` (default): the record is written and nothing is paused; ``enforce`` pauses a killed strategy."""
    return (
        "enforce"
        if os.environ.get("CIO_KEEP_KILL_MODE", "log_only").strip().lower() == "enforce"
        else "log_only"
    )


def t_cdf(x: float, df: float) -> float:
    """CDF of Student's t with ``df`` degrees of freedom."""
    if df <= 0:
        raise ValueError("df must be positive")
    if x == 0:
        return 0.5
    tail = 0.5 * beta_cdf(df / (df + x * x), df / 2.0, 0.5)
    return 1.0 - tail if x > 0 else tail


def t_quantile(p: float, df: float) -> float:
    """The p-quantile of Student's t (bisection on the CDF)."""
    lo, hi = -1e3, 1e3
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def prob_positive_mean(net_r: list[float]) -> float | None:
    """P(mean net R > 0): the Student-t posterior (flat prior) of the mean of the closed rounds."""
    n = len(net_r)
    if n < MIN_ROUNDS_TO_JUDGE:
        return None
    m, s = mean(net_r), stdev(net_r)
    if s == 0:
        return 1.0 if m > 0 else 0.0 if m < 0 else 0.5
    return t_cdf(m / (s / math.sqrt(n)), n - 1)


def lower_bound(net_r: list[float], confidence: float) -> float | None:
    """The lower ``confidence`` bound of the mean net R."""
    n = len(net_r)
    if n < MIN_ROUNDS_TO_JUDGE:
        return None
    s = stdev(net_r)
    return mean(net_r) - t_quantile(confidence, n - 1) * s / math.sqrt(n)


@dataclass
class StrategyScore:
    """A strategy's net-of-cost closed rounds (the scorecard input)."""

    strategy_id: str
    net_r: list[float]
    cumulative_net_loss_usd: float | None = (
        None  # positive number = loss since probation started
    )
    closed_rounds: int | None = None

    @property
    def n(self) -> int:
        return len(self.net_r)


@dataclass
class KeepKillRecord:
    """The daily record of one strategy: the inputs, their sources and the decision."""

    strategy_id: str
    action: Literal["keep", "kill", "watch", "not_evaluated"]
    reason: str
    method: Literal["derived", "fallback", "none"] = "none"
    n: int = 0
    mean_net_r: float | None = None
    cumulative_net_r: float | None = None
    lower_bound_80: float | None = None
    prob_net_ev_positive: float | None = None
    fdr_budget: float | None = None
    strategies_evaluated: int | None = None
    alpha_adj: float | None = None
    alpha_kill: float | None = None
    on_probation: bool = False
    strategies_on_probation: int | None = None
    probation_budget_usd: float | None = None
    probation_budget_source: str = "unavailable"
    cumulative_net_loss_usd: float | None = None
    reduce_step: float | None = None
    equity_usd: float | None = None
    fallbacks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def probation_budget(
    reduce_step: float | None, equity_usd: float | None, strategies_on_probation: int
) -> float | None:
    """Reduce-step x equity / the number of strategies on probation (a loss budget in USD)."""
    if (
        reduce_step is None
        or equity_usd is None
        or equity_usd <= 0
        or strategies_on_probation < 1
    ):
        return None
    return reduce_step * equity_usd / strategies_on_probation


def evaluate_strategies(
    scores: list[StrategyScore],
    *,
    reduce_step: float | None,
    equity_usd: float | None,
    reduce_step_source: str = "drawdown",
) -> list[KeepKillRecord]:
    """The daily keep/kill records for every strategy."""
    fdr = fdr_budget()
    judged = [s for s in scores if s.n >= DERIVED_MIN_ROUNDS]
    n_eval = len(judged)
    probation = [
        s
        for s in scores
        if (s.closed_rounds if s.closed_rounds is not None else s.n)
        < PROBATION_MIN_ROUNDS
    ]
    budget = probation_budget(reduce_step, equity_usd, len(probation))
    alpha_adj = fdr / n_eval if n_eval else None
    alpha_kill = alpha_adj
    probation_ids = {s.strategy_id for s in probation}
    records: list[KeepKillRecord] = []
    for score in scores:
        rec = KeepKillRecord(
            strategy_id=score.strategy_id,
            action="not_evaluated",
            reason="too_few_closed_rounds",
            n=score.n,
            fdr_budget=fdr,
            strategies_evaluated=n_eval,
            alpha_adj=alpha_adj,
            alpha_kill=alpha_kill,
            on_probation=score.strategy_id in probation_ids,
            strategies_on_probation=len(probation),
            reduce_step=reduce_step,
            equity_usd=equity_usd,
            cumulative_net_loss_usd=score.cumulative_net_loss_usd,
        )
        if rec.on_probation and budget is not None:
            rec.probation_budget_usd = budget
            rec.probation_budget_source = reduce_step_source
        if score.n == 0:
            records.append(rec)
            continue
        rec.mean_net_r = mean(score.net_r)
        rec.cumulative_net_r = sum(score.net_r)
        # The budget kill needs only the loss, not a posterior
        if (
            rec.on_probation
            and budget is not None
            and score.cumulative_net_loss_usd is not None
            and score.cumulative_net_loss_usd >= budget
        ):
            rec.action, rec.method = "kill", "derived"
            rec.reason = "probation_budget_exhausted"
            records.append(rec)
            continue
        if score.n < MIN_ROUNDS_TO_JUDGE:
            records.append(rec)
            continue
        rec.prob_net_ev_positive = prob_positive_mean(score.net_r)
        rec.lower_bound_80 = lower_bound(score.net_r, FALLBACK_LOWER_BOUND_CONFIDENCE)
        derived_ok = (
            score.n >= DERIVED_MIN_ROUNDS
            and alpha_adj is not None
            and rec.prob_net_ev_positive is not None
        )
        if derived_ok:
            rec.method = "derived"
            assert alpha_adj is not None and rec.prob_net_ev_positive is not None
            if rec.prob_net_ev_positive >= 1.0 - alpha_adj:
                rec.action, rec.reason = "keep", "p_net_ev_positive_above_fdr_threshold"
            elif rec.prob_net_ev_positive <= (alpha_kill or 0.0):
                rec.action, rec.reason = (
                    "kill",
                    "p_net_ev_positive_at_or_below_alpha_kill",
                )
            else:
                rec.action, rec.reason = "watch", "inside_the_fdr_band"
            if rec.on_probation and budget is None:
                rec.fallbacks.append("probation_budget_unavailable")
        else:
            # Labelled fallback: keep at n >= 100 with the lower 80% bound above 0; kill at -10R or a lower
            # bound below -0.25R.
            rec.method = "fallback"
            rec.fallbacks.append(f"fewer_than_{DERIVED_MIN_ROUNDS}_closed_rounds")
            lb = rec.lower_bound_80
            if (
                rec.cumulative_net_r is not None
                and rec.cumulative_net_r <= FALLBACK_KILL_CUMULATIVE_R
            ):
                rec.action, rec.reason = "kill", "fallback_cumulative_below_minus_10r"
            elif lb is not None and lb < FALLBACK_KILL_LOWER_BOUND_R:
                rec.action, rec.reason = (
                    "kill",
                    "fallback_lower_bound_below_minus_0_25r",
                )
            elif score.n >= FALLBACK_KEEP_MIN_ROUNDS and lb is not None and lb > 0.0:
                rec.action, rec.reason = "keep", "fallback_n_100_lower_bound_positive"
            else:
                rec.action, rec.reason = "watch", "fallback_inside_the_band"
        records.append(rec)
    return records
