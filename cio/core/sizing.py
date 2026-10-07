"""Continuous posterior sizing (petrosa-cio#297, rule 2 of PetroSa2/petrosa_k8s#1239). Pure, no I/O.

    size = max(probe, f_q x Kelly(p_post, b_net) x equity x P(net EV > 0))

p_post is the posterior mean of the strategy's win rate (a Beta prior centred on the order's break-even win rate
with strength k, plus the closed rounds: the posterior of the net-EV gate), b_net the net payoff odds
(take-profit - c) / (S_eff + c) with the stop that will really be placed, P(net EV > 0) the gate's
P(win rate > p_be). No tiers: a small posterior edge gives a size near the probe, a strong one more, capped by
the position limit. ``probe`` is the symbol's smallest valid order (tradeengine's probe mode).
"""

from __future__ import annotations

import os

from cio.core.net_ev import probe_notional
from cio.models.context import TriggerContext
from cio.models.net_ev import NetEvGate, SizingRecord

DEFAULT_KELLY_FRACTION = 0.25  # f_q: the operator's input, confirmed 0.25


def kelly_multiplier() -> float:
    """f_q (``CIO_KELLY_FRACTION``, default 0.25)."""
    try:
        value = float(os.environ["CIO_KELLY_FRACTION"])
    except (KeyError, ValueError):
        return DEFAULT_KELLY_FRACTION
    return value if 0.0 < value <= 1.0 else DEFAULT_KELLY_FRACTION


def kelly_fraction(p: float, b_net: float) -> float:
    """The Kelly fraction p - (1 - p) / b for net odds b; 0 when there is no edge or no payoff."""
    if b_net <= 0:
        return 0.0
    return max(0.0, p - (1.0 - p) / b_net)


def size_order(context: TriggerContext, gate: NetEvGate) -> SizingRecord:
    """Size the order from the gate's posterior; the probe when there is no posterior or the data is flagged."""
    probe = probe_notional(context)
    equity = max(0.0, float(context.available_capital_usd or 0.0))
    f_q = kelly_multiplier()
    record = SizingRecord(
        probe_usd=probe,
        equity_usd=equity,
        f_q=f_q,
        final_size_usd=probe,
        binding="probe",
    )
    posterior = gate.posterior
    if posterior is not None:
        record.p_post = posterior.mean
        record.k = posterior.prior_strength
        record.k_source = posterior.k_source
    if gate.integrity is not None:
        record.reason = "data_integrity_flag"
        return record
    if (
        posterior is None
        or gate.prob_edge is None
        or gate.take_profit_pct is None
        or gate.s_eff is None
        or gate.cost_total is None
    ):
        record.reason = "no_posterior"
        return record
    b_net = (gate.take_profit_pct - gate.cost_total) / (gate.s_eff + gate.cost_total)
    kelly = kelly_fraction(posterior.mean, b_net)
    raw = f_q * kelly * equity * gate.prob_edge
    record.b_net = b_net
    record.prob_net_ev_positive = gate.prob_edge
    record.kelly_fraction = kelly
    record.kelly_size_usd = raw
    cap = context.risk_limits.max_position_size_usd
    capped = min(raw, cap) if cap > 0 else raw
    if capped > probe:
        record.final_size_usd = capped
        record.binding = "max_position" if raw > cap > 0 else "kelly"
    else:
        record.final_size_usd = probe
        record.binding = "probe"
    return record
