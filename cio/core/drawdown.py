"""Drawdown steps at z sigma (petrosa-cio#298, rule 5 of PetroSa2/petrosa_k8s#1239). Pure, no I/O.

New-entry size is reduced (x0.5) when the drawdown from the equity peak reaches z_reduce x sigma and new entries
are halted at z_halt x sigma (z = 2 and 3, confirmed), where

    sigma = max(model sigma, realized sigma of the equity curve), both of the portfolio's daily P&L as a
    fraction of equity;
    model sigma = |net notional / equity| x sigma of the equal-weight basket of the traded pairs, from their
    realized daily sigmas and measured correlations (data-manager's risk inputs).

A component with insufficient inputs is left out; with none, the labelled fallback applies: reduce at 3%, halt at
6%. Closes and reduce-only orders are never blocked. The daily realized-loss stop stays a separate backstop.
"""

from __future__ import annotations

import math
import os
from typing import Any

from cio.models.net_ev import DrawdownDecision, DrawdownState, RiskInputs

DEFAULT_Z_REDUCE = 2.0
DEFAULT_Z_HALT = 3.0
DEFAULT_REDUCE_FACTOR = 0.5
FALLBACK_REDUCE = 0.03
FALLBACK_HALT = 0.06


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    return value if low < value <= high else default


def z_reduce() -> float:
    return _env_float("CIO_DRAWDOWN_Z_REDUCE", DEFAULT_Z_REDUCE, 0.0, 20.0)


def z_halt() -> float:
    return _env_float("CIO_DRAWDOWN_Z_HALT", DEFAULT_Z_HALT, 0.0, 20.0)


def reduce_factor() -> float:
    """The size multiplier at the reduce step (``CIO_DRAWDOWN_REDUCE_FACTOR``, default 0.5)."""
    return _env_float("CIO_DRAWDOWN_REDUCE_FACTOR", DEFAULT_REDUCE_FACTOR, 0.0, 1.0)


def drawdown_enforced() -> bool:
    """False only when ``CIO_DRAWDOWN_MODE=log_only``: the step is computed and logged, never acts."""
    return os.environ.get("CIO_DRAWDOWN_MODE", "enforce").strip().lower() != "log_only"


def is_closing_intent(payload: dict[str, Any]) -> bool:
    """A close or reduce-only intent: never blocked or reduced by the drawdown steps."""
    if payload.get("reduce_only") is True:
        return True
    for key in ("side", "action", "signal_type"):
        token = str(payload.get(key) or "").strip().lower()
        if token.startswith("close"):
            return True
    return False


def basket_sigma(inputs: RiskInputs) -> tuple[float | None, list[str], str | None]:
    """Sigma of the equal-weight basket of the pairs with a sufficient daily sigma, from the measured
    correlations: sqrt(sum_ij rho_ij s_i s_j) / N. None when any pair of them has no sufficient correlation."""
    symbols = sorted(inputs.sigma_daily)
    if not symbols:
        return None, [], "no_pair_has_a_sufficient_daily_sigma"
    total = 0.0
    for i, a in enumerate(symbols):
        for j, b in enumerate(symbols):
            if i == j:
                rho = 1.0
            else:
                rho = (inputs.correlation.get(a) or {}).get(b)
                if rho is None:
                    return None, symbols, f"no_sufficient_correlation_{a}_{b}"
            total += rho * inputs.sigma_daily[a] * inputs.sigma_daily[b]
    return math.sqrt(max(total, 0.0)) / len(symbols), symbols, None


def evaluate_drawdown(
    drawdown: DrawdownState | None, inputs: RiskInputs | None
) -> DrawdownDecision:
    """The drawdown step in force: ``none``, ``reduce`` or ``halt`` (``not_evaluated`` without a drawdown)."""
    zr, zh = z_reduce(), z_halt()
    decision = DrawdownDecision(
        action="not_evaluated",
        z_reduce=zr,
        z_halt=zh,
        reduce_factor=reduce_factor(),
        reduce_threshold=FALLBACK_REDUCE,
        halt_threshold=FALLBACK_HALT,
        threshold_source="fallback",
        sigma_source="fallback",
    )
    if drawdown is None or drawdown.from_peak is None:
        decision.reason = "drawdown_unavailable"
        return decision
    decision.drawdown = drawdown.from_peak
    decision.net_notional_ratio = drawdown.net_notional_ratio

    model = realized = None
    if inputs is not None:
        if drawdown.net_notional_ratio is not None:
            basket, symbols, why = basket_sigma(inputs)
            decision.basket_sigma = basket
            decision.basket_symbols = symbols
            if basket is not None:
                model = abs(drawdown.net_notional_ratio) * basket
            else:
                decision.fallbacks.append(f"model_sigma_{why}")
        else:
            decision.fallbacks.append("model_sigma_net_notional_unavailable")
        if inputs.equity_sufficient and inputs.equity_sigma is not None:
            realized = inputs.equity_sigma
        else:
            decision.fallbacks.append("realized_sigma_insufficient")
    else:
        decision.fallbacks.append("risk_inputs_unavailable")
    decision.model_sigma, decision.realized_sigma = model, realized

    usable = [(v, name) for v, name in ((model, "model"), (realized, "realized")) if v]
    if usable:
        sigma, source = max(usable)
        decision.sigma, decision.sigma_source = sigma, source
        decision.reduce_threshold = zr * sigma
        decision.halt_threshold = zh * sigma
        decision.threshold_source = "derived"
    else:
        decision.fallbacks.append("thresholds_fallback")
    if drawdown.from_peak >= decision.halt_threshold:
        decision.action, decision.reason = "halt", "drawdown_halt"
    elif drawdown.from_peak >= decision.reduce_threshold:
        decision.action, decision.reason = "reduce", "drawdown_reduce"
    else:
        decision.action, decision.reason = "none", "within_steps"
    return decision
