"""Regime policy (petrosa-cio#294; decisions 3, 21 and 22 of PetroSa2/petrosa_k8s#1239). Pure.

* **Low-confidence regime** (decision 22): treated as unavailable: the order goes at **probe size only**, and the
  reason is recorded (on the sizing record and as a context gap).
* **Stale regime** (decision 3): older than max(3 x the analyzer interval, 1 h): also unavailable, so probe size only.
* **turbulent_illiquidity** (decision 21): no block. The cost uplift is the *measured* per-regime slippage of
  PetroSa2/petrosa-data-manager#535 (which the net-EV gate already uses); only until enough fills exist is the
  documented fallback applied: twice the slippage and +0.05R on the required EV (``net_ev.py``).
* **What "low" means** is an explicit, labelled input: ``CIO_REGIME_MIN_CONFIDENCE`` (default 0.70, source
  ``fallback``). The confidence seen, the minimum applied and its source are recorded with ``regime_reason`` on the
  sizing record.
* The CAPITULATION and CHOPPY hard blocks stay (decision 3), and they act **only on a confident regime**: a
  low-confidence one (data-manager reports ``transitional`` at a constant 0.6, which maps to CHOPPY/low) is
  unavailable, not blocking. They were inert for that reason: this policy makes the low-confidence path explicit
  instead of silent.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from cio.models.enums import ConfidenceLevel
from cio.models.net_ev import RegimeAvailability
from cio.models.regime import RegimeResult, regime_min_confidence

DEFAULT_ANALYZER_INTERVAL_SECONDS = 900.0  # data-manager ANALYTICS_INTERVAL
STALE_FLOOR_SECONDS = 3600.0
STALE_INTERVALS = 3.0


def analyzer_interval_seconds() -> float:
    """The data-manager analytics interval (``CIO_REGIME_ANALYZER_INTERVAL_SECONDS``, default 900)."""
    try:
        value = float(os.environ["CIO_REGIME_ANALYZER_INTERVAL_SECONDS"])
    except (KeyError, ValueError):
        return DEFAULT_ANALYZER_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_ANALYZER_INTERVAL_SECONDS


def stale_after_seconds() -> float:
    """max(3 x the analyzer interval, 1 h)."""
    return max(STALE_INTERVALS * analyzer_interval_seconds(), STALE_FLOOR_SECONDS)


def regime_availability(
    regime: RegimeResult, now: datetime | None = None
) -> RegimeAvailability:
    """Whether the regime can inform a decision: confident enough and fresh enough."""
    now = now or datetime.now(UTC)
    stale_after = stale_after_seconds()
    age = None
    computed_at = regime.computed_at
    if computed_at is not None:
        stamp = computed_at if computed_at.tzinfo else computed_at.replace(tzinfo=UTC)
        age = max(0.0, (now - stamp).total_seconds())
    minimum, minimum_source = regime_min_confidence()
    reason = None
    if regime.regime_confidence == ConfidenceLevel.LOW:
        reason = "regime_low_confidence"
    elif age is not None and age > stale_after:
        reason = "regime_stale"
    return RegimeAvailability(
        available=reason is None,
        reason=reason,
        confidence=str(regime.regime_confidence),
        confidence_value=regime.confidence_value,
        min_confidence=minimum,
        min_confidence_source=minimum_source,  # type: ignore[arg-type]
        age_seconds=age,
        stale_after_seconds=stale_after,
        computed_at=computed_at,
        age_known=age is not None,
    )
