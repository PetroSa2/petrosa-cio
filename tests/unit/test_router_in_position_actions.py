"""Tests for in-position router action dispatch (#134, P1.4-AC5 + AC6).

Verifies the router emits MODIFY_STOPS / EXIT_NOW / SCALE_OUT on the
dedicated ``cio.position.<kind>.<strategy_id>`` NATS subjects, that the
shared audit copy on ``cio.decision.audit.<action>`` is published (AC6.a),
and that EXIT_NOW additionally emits the FR66 governance alert family on
``alerts.cio.exit_now.<strategy_id>`` (per #139).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

IN_POSITION_ACTIONS = [
    ActionType.MODIFY_STOPS,
    ActionType.EXIT_NOW,
    ActionType.SCALE_OUT,
]


def _make_decision(action: ActionType) -> DecisionResult:
    return DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=action,
        justification="in-position reasoning",
        thought_trace="audit trace",
    )


def _make_context(strategy_id: str) -> TriggerContext:
    ctx = MagicMock(spec=TriggerContext)
    ctx.strategy_id = strategy_id
    ctx.decision_id = "decision-134"
    ctx.correlation_id = "corr-134"
    ctx.trigger_payload = {"symbol": "BTCUSDT", "position_id": "POS-1"}
    return ctx


# ---------------------------------------------------------------------------
# AC5.a — Enum vocabulary
# ---------------------------------------------------------------------------


def test_in_position_action_types_exposed():
    """Enum carries the three in-position values added by #134."""
    assert ActionType.MODIFY_STOPS.value == "modify_stops"
    assert ActionType.EXIT_NOW.value == "exit_now"
    assert ActionType.SCALE_OUT.value == "scale_out"


# ---------------------------------------------------------------------------
# AC5.b — NATS dispatch subjects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_subject"),
    [
        (ActionType.MODIFY_STOPS, "cio.position.modify_stops"),
        (ActionType.EXIT_NOW, "cio.position.exit_now"),
        (ActionType.SCALE_OUT, "cio.position.scale_out"),
    ],
)
async def test_in_position_action_publishes_on_position_subject(
    action: ActionType, expected_subject: str
):
    """Each in-position action publishes to cio.position.<kind>.<strategy_id>."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    strategy_id = "momentum_pulse"
    context = _make_context(strategy_id)
    decision = _make_decision(action)

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(context, decision)

    calls = {c.args[0]: c.args[1] for c in mock_nc.publish.call_args_list}
    full_subject = f"{expected_subject}.{strategy_id}"
    assert full_subject in calls, (
        f"Expected publish to {full_subject}, got {list(calls.keys())}"
    )


# ---------------------------------------------------------------------------
# AC6.a — Per-action audit copy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", IN_POSITION_ACTIONS)
async def test_in_position_action_emits_audit_copy(action: ActionType):
    """Every in-position dispatch fires the shared cio.decision.audit.<action> copy
    so data-manager's FR12 consumer (P7.1, #610) and the dashboard receive it."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    strategy_id = "momentum_pulse"
    context = _make_context(strategy_id)
    decision = _make_decision(action)

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(context, decision)

    audit_subject = f"cio.decision.audit.{action.value}"
    calls = {c.args[0]: c.args[1] for c in mock_nc.publish.call_args_list}
    assert audit_subject in calls, (
        f"Expected audit copy on {audit_subject}, got {list(calls.keys())}"
    )


# ---------------------------------------------------------------------------
# EXIT_NOW additionally fires the FR66 alert family (#139)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_now_also_emits_fr66_governance_alert():
    """EXIT_NOW is in CIO_ALERT_ACTIONS, so it must additionally publish to
    alerts.cio.exit_now.<strategy_id> on top of the position dispatch + audit
    copy."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    strategy_id = "momentum_pulse"
    context = _make_context(strategy_id)
    decision = _make_decision(ActionType.EXIT_NOW)

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(context, decision)

    subjects = {c.args[0] for c in mock_nc.publish.call_args_list}
    assert f"cio.position.exit_now.{strategy_id}" in subjects
    assert "cio.decision.audit.exit_now" in subjects
    assert f"alerts.cio.exit_now.{strategy_id}" in subjects


# ---------------------------------------------------------------------------
# DRY_RUN must NOT publish (parity with other action families)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", IN_POSITION_ACTIONS)
async def test_in_position_action_suppressed_under_dry_run(action: ActionType):
    """Shadow mode must not emit any NATS publish."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    context = _make_context("momentum_pulse")
    decision = _make_decision(action)

    with patch.dict(os.environ, {"DRY_RUN": "true"}):
        await router.route(context, decision)

    assert mock_nc.publish.call_count == 0
