"""FR66 alert producers (P8-AC2a + AC2b, #139).

Two emit paths land in this module:

- ``alerts.evaluator.unhealthy.<subsystem>`` — fired by
  :class:`cio.core.evaluator_subscriber.EvaluatorSubscriber` when any
  subsystem verdict transitions from non-unhealthy to ``unhealthy``.
- ``alerts.cio.<action>.<strategy_id>`` — fired by
  :class:`cio.core.router.OutputRouter` after dispatching any of the
  governance ActionTypes ``VETO`` / ``DEMOTE`` / ``RETIRE`` /
  ``EXIT_NOW``.

Both paths share the AC2.c payload schema and the same
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
    return ts.replace(microsecond=0).isoformat() + "Z"


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


def evaluator_unhealthy_subject(subsystem: str) -> str:
    safe = (subsystem or "unknown").strip() or "unknown"
    return f"alerts.evaluator.unhealthy.{safe}"


def cio_action_subject(action: str, strategy_id: str) -> str:
    safe_action = (action or "unknown").lower()
    safe_strategy = (strategy_id or "unknown").strip() or "unknown"
    return f"alerts.cio.{safe_action}.{safe_strategy}"


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
