"""Regime policy (petrosa-cio#294; decisions 3, 21 and 22 of PetroSa2/petrosa_k8s#1239). Pure.

* **Low-confidence regime** (decision 22): treated as unavailable: the order goes at **probe size only**, and the
  reason is recorded (on the sizing record and as a context gap).
* **Stale regime** (decision 3): older than max(3 x the analyzer interval, 1 h): also unavailable, so probe size only.
  The age is now minus the time data-manager computed the regime (its ``metadata.timestamp``; a naive time is UTC);
  the interval is ``CIO_REGIME_ANALYZER_INTERVAL_SECONDS`` or its labelled 900 s fallback. An unknown time is not
  called stale.
* **Unknown age** (data-manager's regime without a computation time): freshness cannot be shown, so it is
  unavailable too (``regime_age_unknown``): probe size, never a block.
* **Missing regime** (the fetch failed or data-manager has none for the pair): neutral, probe size only
  (``regime_missing``); never a block and never an error.
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

import logging
import os
import time
from datetime import UTC, datetime

from cio.models.enums import ConfidenceLevel
from cio.models.net_ev import RegimeAvailability
from cio.models.regime import RegimeResult, regime_min_confidence

logger = logging.getLogger(__name__)

#: A regime computed more than this far ahead of now is clock skew worth a warning (the age is clamped to 0)
FUTURE_SKEW_WARN_SECONDS = 300.0
_last_skew_warning: float | None = None

DEFAULT_ANALYZER_INTERVAL_SECONDS = 900.0  # data-manager ANALYTICS_INTERVAL
STALE_FLOOR_SECONDS = 3600.0
STALE_INTERVALS = 3.0


#: How a regime fetch that returned nothing usable looks (``RegimeResult`` safe defaults of the context builder)
MISSING_SIGNALS = frozenset(
    {"data_manager_empty", "data_manager_unknown", "timeout", "error"}
)


def analyzer_interval() -> tuple[float, str]:
    """(seconds, source): the data-manager analytics interval, ``CIO_REGIME_ANALYZER_INTERVAL_SECONDS`` (source
    ``env``) or the 900 s fallback (source ``fallback``)."""
    try:
        value = float(os.environ["CIO_REGIME_ANALYZER_INTERVAL_SECONDS"])
    except (KeyError, ValueError):
        return DEFAULT_ANALYZER_INTERVAL_SECONDS, "fallback"
    if value > 0:
        return value, "env"
    return DEFAULT_ANALYZER_INTERVAL_SECONDS, "fallback"


def analyzer_interval_seconds() -> float:
    return analyzer_interval()[0]


def stale_after_seconds() -> float:
    """max(3 x the analyzer interval, 1 h)."""
    return max(STALE_INTERVALS * analyzer_interval_seconds(), STALE_FLOOR_SECONDS)


def _warn_future_timestamp(stamp: datetime, now: datetime) -> None:
    """Warn at most once per analyzer interval that the regime claims to be computed in the future."""
    global _last_skew_warning
    tick = time.monotonic()
    if (
        _last_skew_warning is not None
        and tick - _last_skew_warning < analyzer_interval_seconds()
    ):
        return
    _last_skew_warning = tick
    logger.warning(
        "REGIME_TIMESTAMP_IN_FUTURE computed_at=%s now=%s: clock skew between data-manager and cio",
        stamp.isoformat(),
        now.isoformat(),
    )


def regime_availability(
    regime: RegimeResult, now: datetime | None = None
) -> RegimeAvailability:
    """Whether the regime can inform a decision: confident enough and fresh enough."""
    now = now or datetime.now(UTC)
    stale_after = stale_after_seconds()
    stale_source = analyzer_interval()[1]
    age = None
    computed_at = regime.computed_at
    if computed_at is not None:
        stamp = computed_at if computed_at.tzinfo else computed_at.replace(tzinfo=UTC)
        raw_age = (now - stamp).total_seconds()
        if raw_age < -FUTURE_SKEW_WARN_SECONDS:
            _warn_future_timestamp(stamp, now)
        age = max(0.0, raw_age)
    minimum, minimum_source = regime_min_confidence()
    reason = None
    if regime.data_manager_regime is None and regime.primary_signal in MISSING_SIGNALS:
        reason = (
            "regime_missing"  # no regime at all: neutral, probe size, never a block
        )
    elif regime.regime_confidence == ConfidenceLevel.LOW:
        reason = "regime_low_confidence"
    elif age is not None and age > stale_after:
        reason = "regime_stale"
    elif age is None and regime.data_manager_regime is not None:
        # data-manager's regime without a computation time: freshness cannot be shown, so it is not trusted
        # (a synthetic regime, e.g. the deterministic bypass one, never came from data-manager and has none)
        reason = "regime_age_unknown"
    return RegimeAvailability(
        available=reason is None,
        reason=reason,
        confidence=str(regime.regime_confidence),
        confidence_value=regime.confidence_value,
        min_confidence=minimum,
        min_confidence_source=minimum_source,  # type: ignore[arg-type]
        age_seconds=age,
        stale_after_seconds=stale_after,
        stale_after_source=stale_source,  # type: ignore[arg-type]
        computed_at=computed_at,
        age_known=age is not None,
    )
