"""Tests for the FR66 alert producer helpers (P8-AC2a / AC2b / AC2c, #139).

Pure tests on the payload-builder + best-effort publish helper. The
wiring side effects in `EvaluatorSubscriber` and `OutputRouter` are
covered by dedicated tests in
`tests/unit/test_evaluator_subscriber_fr66_alert.py` and
`tests/unit/test_router_fr66_alert.py`.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from cio.core.alerting.fr66_alerts import (
    CATEGORY_CIO_GOVERNANCE_ACTION,
    CATEGORY_EVALUATOR_UNHEALTHY,
    CIO_ALERT_ACTIONS,
    SEVERITY_CRITICAL,
    build_cio_action_alert,
    build_evaluator_unhealthy_alert,
    cio_action_subject,
    evaluator_unhealthy_subject,
    publish_fr66_alert,
)

# ---------------------------------------------------------------------------
# AC2.b — action vocabulary


def test_cio_alert_actions_set_covers_the_four_governance_actions():
    """AC2.b enumerates VETO / DEMOTE / RETIRE / EXIT_NOW (lowercased)."""
    assert CIO_ALERT_ACTIONS == frozenset({"veto", "demote", "retire", "exit_now"})


# ---------------------------------------------------------------------------
# AC2.c — payload schema for AC2.a / AC2.b


def test_evaluator_unhealthy_payload_includes_all_required_fields():
    payload = build_evaluator_unhealthy_alert(
        subsystem="ingest",
        reason="lag > 30s",
        previous_verdict="healthy",
        observed_at=datetime(2026, 5, 28, 12, 0, 0),
    )
    assert payload["category"] == CATEGORY_EVALUATOR_UNHEALTHY
    assert payload["severity"] == SEVERITY_CRITICAL
    assert payload["subsystem"] == "ingest"
    assert payload["decision_id"] is None
    assert payload["timestamp"].startswith("2026-05-28T12:00:00")
    assert "ingest" in payload["message"]
    assert "healthy" in payload["message"]  # previous verdict surfaces
    assert "lag" in payload["message"]
    assert payload["dedupe_key"]


def test_cio_action_payload_includes_all_required_fields():
    payload = build_cio_action_alert(
        action="demote",
        strategy_id="alpha-v1",
        decision_id="dec-123",
        justification="strategy unhealthy",
        observed_at=datetime(2026, 5, 28, 12, 0, 0),
    )
    assert payload["category"] == CATEGORY_CIO_GOVERNANCE_ACTION
    assert payload["severity"] == SEVERITY_CRITICAL
    assert payload["strategy_id"] == "alpha-v1"
    assert payload["action"] == "demote"
    assert payload["decision_id"] == "dec-123"
    assert payload["timestamp"].startswith("2026-05-28T12:00:00")
    assert "alpha-v1" in payload["message"]
    assert "DEMOTE" in payload["message"]
    assert payload["dedupe_key"]


def test_dedupe_key_is_stable_for_same_inputs_and_differs_otherwise():
    a = build_evaluator_unhealthy_alert(
        subsystem="ingest",
        reason="r",
        previous_verdict="healthy",
        observed_at=datetime(2026, 5, 28, 12, 0, 0),
    )
    b = build_evaluator_unhealthy_alert(
        subsystem="ingest",
        reason="r",
        previous_verdict="healthy",
        observed_at=datetime(2026, 5, 28, 12, 0, 0),
    )
    c = build_evaluator_unhealthy_alert(
        subsystem="audit",
        reason="r",
        previous_verdict="healthy",
        observed_at=datetime(2026, 5, 28, 12, 0, 0),
    )
    assert a["dedupe_key"] == b["dedupe_key"]
    assert a["dedupe_key"] != c["dedupe_key"]


# ---------------------------------------------------------------------------
# Subject composition


def test_evaluator_subject_appends_subsystem():
    assert evaluator_unhealthy_subject("ingest") == (
        "alerts.evaluator.unhealthy.ingest"
    )


def test_evaluator_subject_falls_back_to_unknown_on_empty():
    assert evaluator_unhealthy_subject("") == ("alerts.evaluator.unhealthy.unknown")


def test_cio_action_subject_lowercases_action_and_keeps_strategy():
    assert cio_action_subject("DEMOTE", "alpha-v1") == ("alerts.cio.demote.alpha-v1")


# ---------------------------------------------------------------------------
# publish_fr66_alert — best-effort behavior


class _StubNATSClient:
    """Captures published payloads in order."""

    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self._raise = raise_on_publish

    async def publish(self, subject: str, payload: bytes) -> None:
        if self._raise:
            raise RuntimeError("boom")
        self.calls.append((subject, payload))


@pytest.mark.asyncio
async def test_publish_returns_true_on_success_and_records_payload():
    client = _StubNATSClient()
    payload = build_evaluator_unhealthy_alert(
        subsystem="ingest",
        reason="lag",
        previous_verdict="healthy",
    )
    ok = await publish_fr66_alert(
        client,
        subject="alerts.evaluator.unhealthy.ingest",
        payload=payload,
    )
    assert ok is True
    assert len(client.calls) == 1
    subject, blob = client.calls[0]
    assert subject == "alerts.evaluator.unhealthy.ingest"
    decoded = json.loads(blob.decode())
    assert decoded["category"] == CATEGORY_EVALUATOR_UNHEALTHY
    assert decoded["subsystem"] == "ingest"


@pytest.mark.asyncio
async def test_publish_returns_false_when_client_is_none_and_does_not_raise():
    payload = build_cio_action_alert(
        action="veto",
        strategy_id="s",
        decision_id="d",
        justification="j",
    )
    ok = await publish_fr66_alert(
        None,
        subject="alerts.cio.veto.s",
        payload=payload,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_publish_returns_false_when_underlying_publish_raises():
    """Best-effort: a NATS hiccup must not bubble up into the caller."""
    client = _StubNATSClient(raise_on_publish=True)
    payload = build_cio_action_alert(
        action="veto",
        strategy_id="s",
        decision_id="d",
        justification="j",
    )
    ok = await publish_fr66_alert(
        client,
        subject="alerts.cio.veto.s",
        payload=payload,
    )
    assert ok is False
