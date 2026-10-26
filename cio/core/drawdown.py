"""Drawdown steps at z sigma (petrosa-cio#298, rule 5 of PetroSa2/petrosa_k8s#1239). Pure, no I/O.

New-entry size is reduced (x0.5) when the drawdown from the equity peak reaches z_reduce x sigma and new entries
are halted at z_halt x sigma (z = 2 and 3, confirmed), where

    sigma = max(model sigma, realized sigma of the equity curve), both of the portfolio's daily P&L as a
    fraction of equity;
    model sigma = |net notional / equity| x sigma of the equal-weight basket of the traded pairs, from their
    realized daily sigmas and measured correlations (data-manager's risk inputs).

A component with insufficient inputs is left out, with one rule on top (petrosa-cio#313): while the **realized**
equity-curve sigma is insufficient the model sigma alone would scale with *current* exposure, while a drawdown
accrues under whatever exposure the book had, so the thresholds are max(derived, fallback): reduce at
max(z_reduce x model sigma, 3%), halt at max(z_halt x model sigma, 6%), source ``fallback_floor``. Once the
realized sigma is sufficient the thresholds are z x max(model, realized) as recorded, with no floor. With no
usable component at all the labelled fallback applies: reduce at 3%, halt at 6%. Closes and reduce-only orders
are never blocked. The daily realized-loss stop stays a separate backstop.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cio.models.net_ev import DrawdownDecision, DrawdownState, RiskInputs

DEFAULT_Z_REDUCE = 2.0
DEFAULT_Z_HALT = 3.0
DEFAULT_REDUCE_FACTOR = 0.5
FALLBACK_REDUCE = 0.03
FALLBACK_HALT = 0.06
DEFAULT_COOLING_SECONDS = 86400.0


@dataclass
class _HaltLifecycle:
    key: str
    started_at: float
    cooling_seconds: float
    resumed_drawdown: float | None = None
    resume_threshold: float | None = None


_HALT_LIFECYCLE: _HaltLifecycle | None = None


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


def _cooling_period(inputs: RiskInputs | None, fallbacks: list[str]) -> float:
    if inputs is not None and inputs.daily_sigma_horizon_days is not None:
        if inputs.daily_sigma_horizon_days > 0:
            return inputs.daily_sigma_horizon_days * 86400.0
    fallbacks.append("cooling_period_daily_sigma_fixed_fallback")
    return _env_float(
        "CIO_DRAWDOWN_COOLING_SECONDS",
        DEFAULT_COOLING_SECONDS,
        0.0,
        31_536_000.0,
    )


def _lifecycle_start(
    drawdown: DrawdownState, current_time: float, fallbacks: list[str]
) -> float:
    if drawdown.halt_started_at is not None:
        return drawdown.halt_started_at
    if drawdown.peak_at:
        try:
            return datetime.fromisoformat(
                drawdown.peak_at.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            pass
    fallbacks.append("cooling_start_process_fallback")
    return current_time


def reset_drawdown_lifecycle() -> None:
    global _HALT_LIFECYCLE
    _HALT_LIFECYCLE = None


def is_closing_intent(payload: dict[str, Any]) -> bool:
    """A close or reduce-only intent: never blocked or reduced by the drawdown steps."""
    if payload.get("reduce_only") is True:
        return True
    for key in ("side", "action", "signal_type"):
        token = str(payload.get(key) or "").strip().lower()
        if token.startswith("close"):
            return True
    return False


BASKET_HELD = "held_pairs"
BASKET_ALL = "basket_all_traded_pairs"


def basket_sigma(
    inputs: RiskInputs, held: dict[str, float] | None = None
) -> tuple[float | None, list[str], str | None, str]:
    """Sigma of the basket of the held pairs, weighted by their |net notional|, from the measured correlations:
    sqrt(sum_ij w_i w_j rho_ij s_i s_j) with w_i = |net_i| / sum |net|.

    Until tradeengine reports the per-symbol net notional (``held`` is None) the basket is every traded pair
    with a sufficient sigma at equal weight, labelled ``basket_all_traded_pairs``: its lower sigma brings the
    steps earlier, which is conservative for a drawdown (the opposite direction holds for exposure caps).
    Returns (sigma, symbols, why-not, basket label).
    """
    if held is None:
        symbols = sorted(inputs.sigma_daily)
        weights = {s: 1.0 / len(symbols) for s in symbols} if symbols else {}
        label = BASKET_ALL
    else:
        symbols = sorted(s for s, n in held.items() if abs(n) > 0)
        total = sum(abs(held[s]) for s in symbols)
        weights = {s: abs(held[s]) / total for s in symbols} if total > 0 else {}
        label = BASKET_HELD
        missing = [s for s in symbols if s not in inputs.sigma_daily]
        if missing:
            return None, symbols, f"no_sufficient_sigma_{'_'.join(missing)}", label
    if not symbols or not weights:
        return (
            None,
            [],
            "no_pair_has_a_sufficient_daily_sigma" if held is None else "no_held_pairs",
            label,
        )
    total_var = 0.0
    for a in symbols:
        for b in symbols:
            if a == b:
                rho = 1.0
            else:
                rho = (inputs.correlation.get(a) or {}).get(b)
                if rho is None:
                    return None, symbols, f"no_sufficient_correlation_{a}_{b}", label
            total_var += (
                weights[a]
                * weights[b]
                * rho
                * inputs.sigma_daily[a]
                * inputs.sigma_daily[b]
            )
    return math.sqrt(max(total_var, 0.0)), symbols, None, label


def evaluate_drawdown(
    drawdown: DrawdownState | None,
    inputs: RiskInputs | None,
    held: dict[str, float] | None = None,
    now: float | None = None,
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
    decision.cooling_period_seconds = _cooling_period(inputs, decision.fallbacks)
    persisted_started_at = drawdown.halt_started_at
    persisted_resumed_drawdown = drawdown.resumed_drawdown
    persisted_resume_threshold = drawdown.resume_threshold

    model = realized = None
    if inputs is not None:
        if drawdown.net_notional_ratio is not None:
            basket, symbols, why, label = basket_sigma(inputs, held)
            decision.basket_sigma = basket
            decision.basket_symbols = symbols
            decision.basket_source = label
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
    decision.components_missing = [
        name for name, v in (("model", model), ("realized", realized)) if not v
    ]

    usable = [(v, name) for v, name in ((model, "model"), (realized, "realized")) if v]
    if usable:
        sigma, source = max(usable)
        decision.sigma, decision.sigma_source = sigma, source
        decision.reduce_threshold = zr * sigma
        decision.halt_threshold = zh * sigma
        decision.threshold_source = "derived"
        if not realized:
            # No equity history to confirm the model sigma: never tighter than the fixed steps (#313)
            decision.reduce_threshold = max(decision.reduce_threshold, FALLBACK_REDUCE)
            decision.halt_threshold = max(decision.halt_threshold, FALLBACK_HALT)
            decision.threshold_source = "fallback_floor"
            decision.fallbacks.append("thresholds_fallback_floor")
    else:
        decision.fallbacks.append("thresholds_fallback")
    lifecycle_key = drawdown.peak_at or (
        str(drawdown.equity_peak) if drawdown.equity_peak is not None else None
    )
    current_time = time.time() if now is None else now
    global _HALT_LIFECYCLE
    if lifecycle_key is not None and persisted_started_at is not None:
        _HALT_LIFECYCLE = _HaltLifecycle(
            lifecycle_key,
            persisted_started_at,
            decision.cooling_period_seconds,
            persisted_resumed_drawdown,
            persisted_resume_threshold,
        )
    if lifecycle_key is not None and _HALT_LIFECYCLE is not None:
        if _HALT_LIFECYCLE.key != lifecycle_key:
            _HALT_LIFECYCLE = None
        elif _HALT_LIFECYCLE.resumed_drawdown is not None:
            decision.halt_started_at = _HALT_LIFECYCLE.started_at
            decision.resumed_drawdown = _HALT_LIFECYCLE.resumed_drawdown
            decision.resume_threshold = _HALT_LIFECYCLE.resume_threshold
            if decision.sigma is not None and drawdown.from_peak >= (
                _HALT_LIFECYCLE.resume_threshold or float("inf")
            ):
                _HALT_LIFECYCLE = _HaltLifecycle(
                    lifecycle_key,
                    current_time,
                    decision.cooling_period_seconds,
                )
                decision.halt_started_at = current_time
                decision.resumed_drawdown = None
                decision.resume_threshold = None
                decision.action, decision.reason = "halt", "drawdown_rehalt"
                decision.lifecycle_event = "rehalt"
                return decision
            if drawdown.from_peak >= decision.reduce_threshold:
                decision.action, decision.reason = "reduce", "drawdown_reduce_resumed"
            else:
                decision.action, decision.reason = "none", "within_steps_resumed"
            return decision
        elif (
            current_time - _HALT_LIFECYCLE.started_at >= decision.cooling_period_seconds
        ):
            _HALT_LIFECYCLE.resumed_drawdown = drawdown.from_peak
            _HALT_LIFECYCLE.resume_threshold = drawdown.from_peak + (
                decision.sigma or decision.halt_threshold / decision.z_halt
            )
            decision.resume_threshold = _HALT_LIFECYCLE.resume_threshold
            decision.halt_started_at = _HALT_LIFECYCLE.started_at
            decision.resumed_drawdown = _HALT_LIFECYCLE.resumed_drawdown
            decision.action, decision.reason = "reduce", "drawdown_resume"
            decision.lifecycle_event = "resume"
            return decision
        elif drawdown.from_peak >= decision.halt_threshold:
            decision.halt_started_at = _HALT_LIFECYCLE.started_at
            decision.action, decision.reason = "halt", "drawdown_halt"
            return decision
    if drawdown.from_peak >= decision.halt_threshold:
        if lifecycle_key is not None:
            _HALT_LIFECYCLE = _HaltLifecycle(
                lifecycle_key,
                _lifecycle_start(drawdown, current_time, decision.fallbacks),
                decision.cooling_period_seconds,
            )
            decision.halt_started_at = _HALT_LIFECYCLE.started_at
            decision.lifecycle_event = "halt"
        decision.action, decision.reason = "halt", "drawdown_halt"
    elif drawdown.from_peak >= decision.reduce_threshold:
        decision.action, decision.reason = "reduce", "drawdown_reduce"
    else:
        decision.action, decision.reason = "none", "within_steps"
    return decision
