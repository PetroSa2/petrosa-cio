"""Models of the net-EV gate: what it saw, what it decided (petrosa-cio#296)."""

from typing import Literal

from pydantic import BaseModel, Field


class CommissionRates(BaseModel):
    """The account's commission for one symbol, as tradeengine reports it on ``/state``."""

    taker_rate: float = Field(..., ge=0.0)
    maker_rate: float = Field(..., ge=0.0)
    source: str = "exchange"
    fetched_at: str | None = None
    fee_burn: bool | None = None


class SlippageEstimate(BaseModel):
    """Measured slippage per fill (basis points, positive = adverse) for the regime in force.

    Read from data-manager's ``/analysis/slippage-by-regime`` (petrosa-data-manager#535).
    """

    regime: str
    median_bp: float
    count: int = Field(..., ge=0)
    source: str = "measured"


class CostComponent(BaseModel):
    """One part of the round-trip cost, as a fraction of the notional, with where it came from."""

    name: str
    value: float
    source: Literal["exchange", "measured", "fallback", "unavailable"]
    detail: str | None = None


class NetEvPosterior(BaseModel):
    """The win-rate posterior the gate judges: a Beta prior centred on p_be plus the closed rounds."""

    wins: int
    losses: int
    prior_strength: float
    alpha: float
    beta: float
    mean: float


class NetEvGate(BaseModel):
    """Decision record of the net-EV gate and the cost-share pre-filter (rules 1, 19, 23).

    ``result`` is ``pass``, ``fail`` or ``not_evaluated`` (the levels or the win rate are unknown, so
    the cold-start path applies and the gate neither passes nor vetoes).
    """

    result: Literal["pass", "fail", "not_evaluated"]
    reason: str
    method: Literal["posterior", "fallback_min_ev", "none"] = "none"
    stop_pct: float | None = None  # carried or configured stop distance
    stop_floor_pct: float | None = None  # tradeengine's floor from /state
    stop_floor_source: str | None = None
    s_eff: float | None = None  # max(stop, floor): the stop that will really be placed
    take_profit_pct: float | None = None  # unchanged by the floor (rule 23, option A)
    reward_risk: float | None = None  # take-profit / s_eff
    costs: list[CostComponent] = Field(default_factory=list)
    cost_total: float | None = None  # c: round-trip cost
    cost_share: float | None = None  # c / s_eff
    p_be: float | None = None  # break-even win rate (s_eff + c) / (s_eff + take_profit)
    posterior: NetEvPosterior | None = None
    prob_edge: float | None = None  # P(win rate > p_be)
    alpha: float | None = None
    net_ev_r: float | None = (
        None  # net EV in R at the posterior mean (or the point win rate)
    )
    min_net_ev_r: float | None = None  # the floor used by the labelled fallback
    p_ref: float | None = None  # the strategy's shrunk win rate
    cost_share_limit: float | None = None  # p_ref * (R + 1) - 1
    cost_share_skip: bool = False  # c/S above the limit
    fallbacks: list[str] = Field(default_factory=list)
