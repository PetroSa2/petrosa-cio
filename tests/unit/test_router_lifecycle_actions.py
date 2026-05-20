"""Per #114 P1.2: lifecycle action types (ADMIT, ADMIT_SMALL, REJECT, PROMOTE,
DEMOTE, RETIRE).

Verifies the router emits each lifecycle action on its dedicated NATS subject
(`cio.lifecycle.<kind>.<strategy_id>`) and that the audit path persists the
new actions through the existing vector-client upsert (no new persistence
code path required, mirroring the #589 governance pattern).
"""

from __future__ import annotations

import json
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

LIFECYCLE_ACTIONS = [
    ActionType.ADMIT,
    ActionType.ADMIT_SMALL,
    ActionType.REJECT,
    ActionType.PROMOTE,
    ActionType.DEMOTE,
    ActionType.RETIRE,
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
        justification="lifecycle reasoning",
        thought_trace="audit trace",
    )


def _make_context(strategy_id: str) -> TriggerContext:
    ctx = MagicMock(spec=TriggerContext)
    ctx.strategy_id = strategy_id
    ctx.decision_id = "decision-114"
    ctx.correlation_id = "corr-114"
    ctx.trigger_payload = {"symbol": "BTCUSDT"}
    return ctx


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


def test_lifecycle_action_types_exposed():
    """Enum carries the six lifecycle values added by P1.2."""
    assert ActionType.ADMIT.value == "admit"
    assert ActionType.ADMIT_SMALL.value == "admit_small"
    assert ActionType.REJECT.value == "reject"
    assert ActionType.PROMOTE.value == "promote"
    assert ActionType.DEMOTE.value == "demote"
    assert ActionType.RETIRE.value == "retire"


# ---------------------------------------------------------------------------
# NATS dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", LIFECYCLE_ACTIONS)
async def test_lifecycle_action_publishes_on_dedicated_subject(action: ActionType):
    """Each lifecycle action publishes to cio.lifecycle.<kind>.<sid> with the decision JSON."""
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

    mock_nc.publish.assert_called_once()
    subject, payload_bytes = mock_nc.publish.call_args.args
    assert subject == f"cio.lifecycle.{action.value}.{strategy_id}"

    payload = json.loads(payload_bytes.decode())
    assert payload["action"] == action.value
    assert payload["justification"] == "lifecycle reasoning"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", LIFECYCLE_ACTIONS)
async def test_lifecycle_action_persists_audit_with_decision_id(action: ActionType):
    """Audit upsert is called for each lifecycle action and carries decision_id."""
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

    mock_vc.upsert.assert_called_once()
    upsert_kwargs = mock_vc.upsert.call_args.kwargs
    assert upsert_kwargs["strategy_id"] == strategy_id
    audit_payload = upsert_kwargs["payload"]
    assert audit_payload["action"] == action.value
    assert audit_payload["decision_id"] == "decision-114"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", LIFECYCLE_ACTIONS)
async def test_lifecycle_action_dry_run_skips_publish(action: ActionType):
    """In DRY_RUN, lifecycle actions log but do not publish (mirrors #589)."""
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

    mock_nc.publish.assert_not_called()
    # Audit still happens in dry-run (it's the durable record of intent).
    mock_vc.upsert.assert_called_once()
