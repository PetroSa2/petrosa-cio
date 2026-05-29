"""FR66 alert producers (P8-AC2a + AC2b, #139; P8-AC2c, #140).

Three emit paths land in this module:

- ``alerts.evaluator.unhealthy.<subsystem>`` — fired by
  :class:`cio.core.evaluator_subscriber.EvaluatorSubscriber` when any
  subsystem verdict transitions from non-unhealthy to ``unhealthy``.
- ``alerts.cio.<action>.<strategy_id>`` — fired by
  :class:`cio.core.router.OutputRouter` after dispatching any of the
  governance ActionTypes ``VETO`` / ``DEMOTE`` / ``RETIRE`` /
  ``EXIT_NOW``.
- ``alerts.drawdown.breach.<strategy_id>`` — fired wherever realized
  drawdown is compared against the strategy's envelope (FR30 / FR62)
  and the p99 ceiling is exceeded. Dedup is keyed on
  ``(strategy_id, position_id)`` and resets when the position exits
  and a new one opens; see
  :class:`cio.core.alerting.drawdown_breach_emitter.DrawdownBreachEmitter`.

All paths share the AC2.c payload schema and the same
``publish_fr66_alert`` helper, so a downstream NATS-to-Grafana bridge
(AC2.d, infra-side) can match on a single subject family
(``alerts.>``) and a single payload shape.

Best-effort publishing is enforced by the helper: failures are logged
and counted via a Prometheus counter but never raised — the producer
paths (evaluator subscription, router dispatch) must not break because
the observability bus hiccupped.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Protocol

from prometheus_client import Counter

logger = logging.getLogger(__name__)


# AC2.c category enum — kept as string constants (not StrEnum) so the
# payload JSON renders the value verbatim.
CATEGORY_EVALUATOR_UNHEALTHY = "evaluator_unhealthy"
CATEGORY_CIO_GOVERNANCE_ACTION = "cio_governance_action"
# P8-AC2c (#140) — drawdown vs. envelope breach (FR66 c / FR30 / FR62).
CATEGORY_DRAWDOWN_ENVELOPE_BREACH = "drawdown_envelope_breach"
# P4.6-AC5 (#152) — characterization-vs-approved envelope drift (FR62 / FR66).
CATEGORY_ENVELOPE_DRIFT_DETECTED = "envelope_drift_detected"

# AC2.b — the four ActionType values that get an alert event.
CIO_ALERT_ACTIONS: frozenset[str] = frozenset({"veto", "demote", "retire", "exit_now"})

# AC2.d wire-up (Grafana mapping) reads `severity`. Keep enum aligned
# with `tests/test_setup_grafana_alerting_fr66.py` in petrosa_k8s.
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


cio_fr66_alerts_published = Counter(
    "cio_fr66_alerts_published_total",
    "Total FR66 alerts published to NATS by the CIO",
    ["category", "severity", "result"],
)


class _NATSPublisher(Protocol):
    """Structural protocol — keeps the helper testable without nats-py."""

    async def publish(self, subject: str, payload: bytes) -> None: ...


def _iso_utc(ts: datetime | None = None) -> str:
    ts = ts or datetime.utcnow()
    # Strip tzinfo so a tz-aware input doesn't render as "+00:00Z".
    ts = ts.replace(microsecond=0, tzinfo=None)
    return ts.isoformat() + "Z"


def _dedupe_key(*parts: str) -> str:
    """Stable short key for the alerts collection (693.5 dedupe column)."""
    joined = "|".join(p or "" for p in parts)
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def build_evaluator_unhealthy_alert(
    *,
    subsystem: str,
    reason: str,
    previous_verdict: str | None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """AC2.c payload for the evaluator-unhealthy transition."""
    detected_at = _iso_utc(observed_at)
    return {
        "category": CATEGORY_EVALUATOR_UNHEALTHY,
        "severity": SEVERITY_CRITICAL,
        "subsystem": subsystem,
        "message": (
            f"Evaluator subsystem '{subsystem}' transitioned to unhealthy "
            f"(previous={previous_verdict or 'none'}). Reason: {reason or 'n/a'}"
        ),
        "decision_id": None,
        "timestamp": detected_at,
        "dedupe_key": _dedupe_key("evaluator_unhealthy", subsystem, detected_at[:13]),
    }


def build_cio_action_alert(
    *,
    action: str,
    strategy_id: str,
    decision_id: str | None,
    justification: str | None,
    severity: str = SEVERITY_CRITICAL,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """AC2.c payload for VETO / DEMOTE / RETIRE / EXIT_NOW dispatches."""
    detected_at = _iso_utc(observed_at)
    return {
        "category": CATEGORY_CIO_GOVERNANCE_ACTION,
        "severity": severity,
        "strategy_id": strategy_id,
        "action": action,
        "message": (
            f"CIO {action.upper()} on strategy_id={strategy_id}. "
            f"Justification: {justification or 'n/a'}"
        ),
        "decision_id": decision_id or None,
        "timestamp": detected_at,
        "dedupe_key": _dedupe_key(
            "cio_action", action, strategy_id, decision_id or detected_at
        ),
    }


def build_drawdown_breach_alert(
    *,
    strategy_id: str,
    position_id: str,
    observed_drawdown: float,
    envelope_p99: float,
    envelope_p100: float | None,
    severity: str = SEVERITY_CRITICAL,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """AC2.c payload for ``alerts.drawdown.breach.<strategy_id>``.

    Carries the AC2.c.2 envelope contract verbatim: ``observed_drawdown``
    is the realized drawdown that exceeded ``envelope_p99``; the optional
    ``envelope_p100`` is the historical worst-case the strategy's
    drawdown envelope ever ran (FR62) so the receiver can rank severity.

    The producer is expected to use
    :class:`cio.core.alerting.drawdown_breach_emitter.DrawdownBreachEmitter`
    to enforce AC2.c.3's dedup window (``(strategy_id, position_id)``
    resets on position exit + new position open).
    """
    detected_at = _iso_utc(observed_at)
    return {
        "category": CATEGORY_DRAWDOWN_ENVELOPE_BREACH,
        "severity": severity,
        "strategy_id": strategy_id,
        "position_id": position_id,
        "observed_drawdown": observed_drawdown,
        "envelope_p99": envelope_p99,
        "envelope_p100": envelope_p100,
        "message": (
            f"Drawdown breach on strategy_id={strategy_id} position_id={position_id}: "
            f"realized={observed_drawdown:.4f} > envelope.p99={envelope_p99:.4f}"
        ),
        "decision_id": None,
        "timestamp": detected_at,
        "dedupe_key": _dedupe_key(
            "drawdown_breach", strategy_id, position_id, detected_at[:13]
        ),
    }


def build_envelope_drift_alert(
    *,
    strategy_key: str,
    current_version: int | None,
    current_value: dict[str, Any] | None,
    proposed_value: dict[str, Any],
    divergence_pct: float,
    originating_characterization_revision: str,
    severity: str = SEVERITY_WARNING,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """AC5.b payload for ``alerts.envelope.drift_detected.<strategy_key>`` (P4.6-AC5).

    Fires when a characterization-emitted envelope diverges from the active
    operator-approved envelope by more than the configured threshold (default
    10%). ``current_value=None`` / ``current_version=None`` means no prior
    approved envelope exists — that branch alerts with the "first approval
    required" semantic so the operator knows a producer is trying to seed
    a fresh envelope without explicit consent.

    AC5.d is enforced **by construction at the call site**: this payload
    carries the proposed values for operator inspection, but the producer
    never mutates the active Envelope. The operator workflow on
    petrosa-data-manager (#187) is the only legitimate write path.
    """
    detected_at = _iso_utc(observed_at)
    if current_value is None:
        message = (
            f"Envelope drift detected on strategy_key={strategy_key}: "
            f"no operator-approved envelope yet exists; characterization "
            f"revision {originating_characterization_revision} proposed a fresh value"
        )
    else:
        message = (
            f"Envelope drift detected on strategy_key={strategy_key}: "
            f"proposed envelope diverges from approved v{current_version} "
            f"by {divergence_pct:.1%} (characterization "
            f"revision {originating_characterization_revision})"
        )
    return {
        "category": CATEGORY_ENVELOPE_DRIFT_DETECTED,
        "severity": severity,
        "strategy_key": strategy_key,
        "current_version": current_version,
        "current_value": current_value,
        "proposed_value": proposed_value,
        "divergence_pct": divergence_pct,
        "originating_characterization_revision": originating_characterization_revision,
        "message": message,
        "decision_id": None,
        "timestamp": detected_at,
        "dedupe_key": _dedupe_key(
            "envelope_drift",
            strategy_key,
            originating_characterization_revision,
            detected_at[:13],
        ),
    }


def evaluator_unhealthy_subject(subsystem: str) -> str:
    safe = (subsystem or "unknown").strip() or "unknown"
    return f"alerts.evaluator.unhealthy.{safe}"


def cio_action_subject(action: str, strategy_id: str) -> str:
    safe_action = (action or "unknown").lower()
    safe_strategy = (strategy_id or "unknown").strip() or "unknown"
    return f"alerts.cio.{safe_action}.{safe_strategy}"


def drawdown_breach_subject(strategy_id: str) -> str:
    """AC2.c.2: ``alerts.drawdown.breach.<strategy_id>``."""
    safe_strategy = (strategy_id or "unknown").strip() or "unknown"
    return f"alerts.drawdown.breach.{safe_strategy}"


def envelope_drift_subject(strategy_key: str) -> str:
    """AC5.b: ``alerts.envelope.drift_detected.<strategy_key>`` (P4.6-AC5).

    ``strategy_key`` is the flat-string partition key from
    :mod:`data_manager.models.envelope` (``strategy:<id>`` or
    ``portfolio:<id>``); the helper passes it through verbatim after the
    same defensive ``strip()/unknown`` shaping the other subjects use.
    """
    safe = (strategy_key or "unknown").strip() or "unknown"
    return f"alerts.envelope.drift_detected.{safe}"


async def publish_fr66_alert(
    nats_client: _NATSPublisher | None,
    *,
    subject: str,
    payload: dict[str, Any],
) -> bool:
    """Publish one FR66 alert. Best-effort: never raises into the caller.

    Returns ``True`` on a successful publish, ``False`` otherwise (NATS
    client absent, publish raised, etc.). The producer is expected to
    log its own intent above this call so an absent / unhealthy NATS
    does not silently drop the operator's "something just happened" signal.
    """
    category = str(payload.get("category", "unknown"))
    severity = str(payload.get("severity", "unknown"))

    if nats_client is None:
        logger.info(
            "fr66_alert.skipped subject=%s category=%s severity=%s "
            "(nats client not wired)",
            subject,
            category,
            severity,
        )
        cio_fr66_alerts_published.labels(
            category=category,
            severity=severity,
            result="skipped",
        ).inc()
        return False

    try:
        await nats_client.publish(subject, json.dumps(payload).encode())
    except Exception as exc:
        logger.warning(
            "fr66_alert.failed subject=%s category=%s severity=%s error=%s",
            subject,
            category,
            severity,
            exc,
        )
        cio_fr66_alerts_published.labels(
            category=category,
            severity=severity,
            result="error",
        ).inc()
        return False

    logger.info(
        "fr66_alert.published subject=%s category=%s severity=%s",
        subject,
        category,
        severity,
    )
    cio_fr66_alerts_published.labels(
        category=category,
        severity=severity,
        result="ok",
    ).inc()
    return True
