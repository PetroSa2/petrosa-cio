"""End-to-end integration test for the in-position re-evaluation loop (#136, AC8 / FR60).

Exercises the loop that #134 (in-position ActionType + NATS dispatch + audit)
and #135 (PositionReviewLoop cadence + backpressure) shipped — the two
pieces that make up the P1.4 in-position governance pipeline producer side.

Scope (AC8.a):
    A position opens → simulated cadence tick fires SCHEDULED_REVIEW →
    a runner (the integration's stand-in for the SignalArbiter +
    OutputRouter wiring at app boot) dispatches an in-position action
    via NATS → the audit copy lands on ``cio.decision.audit.<action>``
    → operator-replay surface (dashboard ring buffer) carries the
    decision.

This test is deliberately in-process: instantiates the real
``PositionReviewLoop`` + ``OutputRouter`` against a recording NATS
client + an in-memory ``DecisionStore`` (the dashboard's ring buffer).
Mirrors AC8.c assertions without booting Mongo, Redis, or a real NATS.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.core.alerting.fr66_alerts import CIO_ALERT_ACTIONS
from cio.core.position_review_loop import PositionKey, PositionReviewLoop
from cio.core.router import OutputRouter
from cio.models import (
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    RegimeFit,
    TriggerContext,
)


class _RecordingNATS:
    """Captures (subject, payload-dict) for every publish."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        try:
            decoded = json.loads(payload.decode())
        except (json.JSONDecodeError, AttributeError):
            decoded = {"raw": payload.decode(errors="replace")}
        self.published.append((subject, decoded))

    def published_to(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        return [(s, p) for s, p in self.published if s.startswith(prefix)]


class _RecordingDecisionStore:
    """In-memory dashboard ring buffer — captures everything the router records."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, record) -> None:
        self.records.append(record)


def _build_router(nats, store=None) -> OutputRouter:
    return OutputRouter(
        nats_client=nats,
        vector_client=AsyncMock(),
        ta_bot_url=None,
        realtime_strategies_url=None,
        decision_store=store or _RecordingDecisionStore(),
    )


def _build_in_position_context(
    *,
    strategy_id: str = "momentum-v3",
    position_id: str = "POS-1234",
):
    """A MagicMock(spec=TriggerContext) — same pattern as test_router_in_position_actions.py."""
    ctx = MagicMock(spec=TriggerContext)
    ctx.strategy_id = strategy_id
    ctx.decision_id = f"dec-{position_id}"
    ctx.correlation_id = f"corr-{position_id}"
    ctx.trigger_payload = {"symbol": "BTCUSDT", "position_id": position_id}
    ctx.pre_decision_context = None
    return ctx


def _build_in_position_decision(action: ActionType) -> DecisionResult:
    """Real DecisionResult — same shape as test_router_in_position_actions.py."""
    return DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=action,
        justification=f"scheduled_review action={action.value}",
        thought_trace=f"in-position re-evaluation produced {action.value}",
    )


# ---------------------------------------------------------------------------
# AC8.c.2 — cadence fires per active position (integration angle on #135)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cadence_fires_one_review_per_active_position():
    fired: list[PositionKey] = []

    async def _runner(key: PositionKey, reason: str) -> None:
        fired.append(key)

    loop = PositionReviewLoop(runner=_runner, interval_seconds=0.05)
    loop.add_position("momentum-v3", "POS-1")
    loop.add_position("meanrev-v1", "POS-2")
    await loop.start()
    await asyncio.sleep(0.18)
    await loop.stop()

    fired_set = set(fired)
    assert PositionKey("momentum-v3", "POS-1") in fired_set
    assert PositionKey("meanrev-v1", "POS-2") in fired_set


# ---------------------------------------------------------------------------
# AC8.a + AC8.c.3 — in-position dispatch lands on NATS + audit channels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_position_dispatch_publishes_position_subject_and_audit():
    nats = _RecordingNATS()
    store = _RecordingDecisionStore()
    router = _build_router(nats, store)

    context = _build_in_position_context()
    decision = _build_in_position_decision(ActionType.MODIFY_STOPS)
    await router.route(context, decision)

    position_emits = nats.published_to(
        f"cio.position.modify_stops.{context.strategy_id}"
    )
    assert len(position_emits) >= 1
    payload = position_emits[0][1]
    assert payload.get("action") == ActionType.MODIFY_STOPS.value

    audit_emits = nats.published_to(
        f"cio.decision.audit.{ActionType.MODIFY_STOPS.value}"
    )
    assert len(audit_emits) >= 1
    audit_payload = audit_emits[0][1]
    assert audit_payload.get("strategy_id") == context.strategy_id
    assert audit_payload.get("action") == ActionType.MODIFY_STOPS.value


@pytest.mark.asyncio
async def test_exit_now_also_fires_fr66_governance_alert():
    """EXIT_NOW is in CIO_ALERT_ACTIONS → in-position subject + alert subject must both fire."""
    nats = _RecordingNATS()
    router = _build_router(nats)

    context = _build_in_position_context(strategy_id="momentum-v3", position_id="POS-7")
    decision = _build_in_position_decision(ActionType.EXIT_NOW)
    await router.route(context, decision)

    assert nats.published_to("cio.position.exit_now.momentum-v3"), (
        "in-position dispatch missing"
    )
    assert nats.published_to("alerts.cio.exit_now.momentum-v3"), (
        "FR66 governance alert missing for EXIT_NOW"
    )
    assert "exit_now" in CIO_ALERT_ACTIONS


# ---------------------------------------------------------------------------
# AC8.c.4 — operator replay (decision_store ring buffer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_decision_store_records_in_position_dispatch():
    nats = _RecordingNATS()
    store = _RecordingDecisionStore()
    router = _build_router(nats, store)

    context = _build_in_position_context(strategy_id="momentum-v3", position_id="POS-9")
    decision = _build_in_position_decision(ActionType.SCALE_OUT)
    await router.route(context, decision)

    assert len(store.records) >= 1
    rec = store.records[-1]
    assert getattr(rec, "action", None) == ActionType.SCALE_OUT.value
    assert getattr(rec, "strategy_id", None) == "momentum-v3"


# ---------------------------------------------------------------------------
# AC8.a closure — full end-to-end cadence → dispatch → replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_in_position_loop_cadence_to_dispatch_to_replay():
    """
    The headline AC8.a slice — one E2E pass through the in-position loop:
      1. Position opens.
      2. Cadence tick fires SCHEDULED_REVIEW (PositionReviewLoop).
      3. Runner translates the cadence call into router.route(...) with
         an in-position action.
      4. Router publishes cio.position.<action> + cio.decision.audit.<action>.
      5. Dashboard ring buffer records the decision for replay.
    """
    nats = _RecordingNATS()
    store = _RecordingDecisionStore()
    router = _build_router(nats, store)

    async def _runner(key: PositionKey, reason: str) -> None:
        context = _build_in_position_context(
            strategy_id=key.strategy_id,
            position_id=key.position_id,
        )
        decision = _build_in_position_decision(ActionType.MODIFY_STOPS)
        await router.route(context, decision)

    loop = PositionReviewLoop(runner=_runner, interval_seconds=0.05)
    loop.add_position("momentum-v3", "POS-42")

    await loop.start()
    await asyncio.sleep(0.12)
    await loop.stop()

    assert nats.published_to("cio.position.modify_stops.momentum-v3"), (
        "cadence never produced an in-position dispatch"
    )
    assert nats.published_to("cio.decision.audit.modify_stops"), (
        "in-position dispatch produced no audit copy"
    )
    assert store.records, "dashboard ring buffer captured nothing"
    rec = store.records[-1]
    assert rec.action == ActionType.MODIFY_STOPS.value
