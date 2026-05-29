"""Envelope-drift alert emitter (P4.6-AC5, #152 — FR62 / FR66).

Owns the AC5.a/b/c/d behaviour for envelope drift detection:

* AC5.a — divergence is computed between a characterization-emitted
  proposed envelope and the active operator-approved envelope; the
  threshold is configurable (default 10%).
* AC5.b — when divergence exceeds the threshold, emits
  ``alerts.envelope.drift_detected.<strategy_key>`` on NATS with the
  payload shaped by :func:`cio.core.alerting.fr66_alerts.build_envelope_drift_alert`.
* AC5.c — alerts are rate-limited per ``strategy_key`` (default
  ``1 / hour``) so a noisy characterization pipeline can't storm the
  operator.
* AC5.d — **by construction**: this emitter has no envelope-mutation
  API. It only inspects values, computes divergence, and publishes
  alerts. The active envelope can only change via the
  petrosa-data-manager#187 operator-approval workflow.

The emitter is process-local and in-memory (same NFR-R1 detection-time
window convention as :class:`cio.core.alerting.drawdown_breach_emitter.DrawdownBreachEmitter`):
the rate-limit table resets on restart, which biases toward "alert at
least once after a restart" rather than "guaranteed no-storm under
crash-loop". Acceptable for an operator-facing signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from cio.core.alerting.fr66_alerts import (
    SEVERITY_WARNING,
    build_envelope_drift_alert,
    envelope_drift_subject,
    publish_fr66_alert,
)

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

if TYPE_CHECKING:
    from cio.core.alerting.fr66_alerts import _NATSPublisher

logger = logging.getLogger(__name__)


DEFAULT_DIVERGENCE_THRESHOLD = 0.10
"""AC5.a default: a proposed value diverging by ≥ 10% from the approved
value is "materially different"."""

DEFAULT_RATE_LIMIT_WINDOW = timedelta(hours=1)
"""AC5.c default: max one envelope-drift alert per ``strategy_key`` per hour."""


def compute_max_divergence_pct(
    current_value: dict[str, Any] | None,
    proposed_value: dict[str, Any],
) -> float:
    """Return the maximum per-key divergence between current and proposed envelopes.

    The schema of an envelope value is open (per
    :mod:`data_manager.models.envelope`) — this helper only attempts to
    measure divergence for **numeric** keys that exist on both sides.
    Each shared numeric key contributes
    ``abs(proposed - current) / max(abs(current), epsilon)``; the helper
    returns the max across keys. Keys present on only one side are treated
    as ``infinity`` divergence (returns ``float('inf')``), so a producer
    adding/removing fields always crosses any sensible threshold.

    Returns ``float('inf')`` when ``current_value is None`` — there's no
    approved envelope to compare against, so AC5.a says "this should alert"
    (the operator has to consciously accept the first envelope).
    """
    if current_value is None:
        return float("inf")
    if not proposed_value and not current_value:
        return 0.0

    current_keys = set(_numeric_keys(current_value))
    proposed_keys = set(_numeric_keys(proposed_value))
    only_current = current_keys - proposed_keys
    only_proposed = proposed_keys - current_keys
    shared = current_keys & proposed_keys

    if only_current or only_proposed:
        # Schema change is always material.
        return float("inf")

    if not shared:
        # No numeric keys to compare — treat as max divergence so the
        # operator still gets notified about the proposed value's shape.
        return float("inf")

    epsilon = 1e-12
    max_div = 0.0
    for key in shared:
        cur = float(current_value[key])
        prop = float(proposed_value[key])
        denom = max(abs(cur), epsilon)
        div = abs(prop - cur) / denom
        if div > max_div:
            max_div = div
    return max_div


def _numeric_keys(value: dict[str, Any]) -> list[str]:
    """Return the keys of ``value`` whose value is numeric (int or float)."""
    return [
        k
        for k, v in value.items()
        if isinstance(v, int | float) and not isinstance(v, bool)
    ]


@dataclass(frozen=True)
class DriftAlertRecord:
    """Last alert event for one ``strategy_key`` (used by the rate-limiter)."""

    strategy_key: str
    fired_at: datetime
    divergence_pct: float
    originating_characterization_revision: str


@dataclass
class EnvelopeDriftEmitter:
    """Stateful emitter for ``alerts.envelope.drift_detected.<strategy_key>``.

    Usage from a producer (e.g. a future characterization-pipeline
    consumer)::

        emitter = EnvelopeDriftEmitter(nats_client=nc)
        emitted = await emitter.check_and_emit(
            strategy_key="strategy:btc_momentum_v3",
            current_version=3,
            current_value={"max_drawdown_pct": 5.0, "stop_loss_pct": 2.0},
            proposed_value={"max_drawdown_pct": 8.0, "stop_loss_pct": 2.0},
            originating_characterization_revision="char-rev-42",
        )

    No envelope is mutated by this class — emitting an alert is the only
    side effect (AC5.d).
    """

    nats_client: _NATSPublisher | None = None
    divergence_threshold: float = DEFAULT_DIVERGENCE_THRESHOLD
    rate_limit_window: timedelta = DEFAULT_RATE_LIMIT_WINDOW
    _last_fired: dict[str, DriftAlertRecord] = field(default_factory=dict, init=False)

    async def check_and_emit(
        self,
        *,
        strategy_key: str,
        current_version: int | None,
        current_value: dict[str, Any] | None,
        proposed_value: dict[str, Any],
        originating_characterization_revision: str,
        severity: str = SEVERITY_WARNING,
        observed_at: datetime | None = None,
    ) -> bool:
        """Emit a drift alert if divergence > threshold AND rate-limit window expired.

        Returns ``True`` if an alert was emitted, ``False`` for any of:

        * divergence ≤ threshold (AC5.a: not material), OR
        * rate-limit window still active for this ``strategy_key`` (AC5.c).

        NATS publish failures are best-effort and do not flip the return
        to ``False`` — the rate-limit record is still updated because the
        operator's "we considered alerting and won't repeat for a window"
        intent doesn't depend on the bus being healthy.
        """
        divergence_pct = compute_max_divergence_pct(current_value, proposed_value)
        now = observed_at or datetime.now(UTC)

        if divergence_pct <= self.divergence_threshold:
            logger.debug(
                "envelope_drift.below_threshold strategy_key=%s divergence=%.4f "
                "threshold=%.4f",
                strategy_key,
                divergence_pct,
                self.divergence_threshold,
            )
            return False

        last = self._last_fired.get(strategy_key)
        if last is not None and (now - last.fired_at) < self.rate_limit_window:
            logger.debug(
                "envelope_drift.rate_limited strategy_key=%s divergence=%.4f "
                "last_fired_at=%s window=%s",
                strategy_key,
                divergence_pct,
                last.fired_at.isoformat(),
                self.rate_limit_window,
            )
            return False

        payload = build_envelope_drift_alert(
            strategy_key=strategy_key,
            current_version=current_version,
            current_value=current_value,
            proposed_value=proposed_value,
            divergence_pct=divergence_pct,
            originating_characterization_revision=originating_characterization_revision,
            severity=severity,
            observed_at=now,
        )
        logger.info(
            "envelope_drift.detected strategy_key=%s divergence=%.4f "
            "current_version=%s char_revision=%s",
            strategy_key,
            divergence_pct,
            current_version,
            originating_characterization_revision,
        )
        await publish_fr66_alert(
            self.nats_client,
            subject=envelope_drift_subject(strategy_key),
            payload=payload,
        )
        self._last_fired[strategy_key] = DriftAlertRecord(
            strategy_key=strategy_key,
            fired_at=now,
            divergence_pct=divergence_pct,
            originating_characterization_revision=originating_characterization_revision,
        )
        return True

    def last_fired_at(self, strategy_key: str) -> datetime | None:
        """Diagnostic: return the last fire time for ``strategy_key``, or None."""
        rec = self._last_fired.get(strategy_key)
        return rec.fired_at if rec is not None else None

    def snapshot(self) -> dict[str, Any]:
        """State export for debug/diagnostics endpoints."""
        return {
            "fired": [
                {
                    "strategy_key": r.strategy_key,
                    "fired_at": r.fired_at.isoformat(),
                    "divergence_pct": r.divergence_pct,
                    "originating_characterization_revision": (
                        r.originating_characterization_revision
                    ),
                }
                for r in self._last_fired.values()
            ],
            "threshold": self.divergence_threshold,
            "rate_limit_window_seconds": self.rate_limit_window.total_seconds(),
        }
