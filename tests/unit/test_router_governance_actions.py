"""Per #589 P1.1: governance action types (DOWN_WEIGHT, THROTTLE, VETO).

Verifies the router emits each governance action on its dedicated NATS subject
and that the audit path persists the new actions through the existing pattern
(no new persistence code path required).
"""

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
        justification="governance reasoning",
        thought_trace="audit trace",
    )


def _make_context(strategy_id: str) -> TriggerContext:
    ctx = MagicMock(spec=TriggerContext)
    ctx.strategy_id = strategy_id
    ctx.decision_id = "decision-589"
    ctx.correlation_id = "corr-589"
    ctx.trigger_payload = {"symbol": "BTCUSDT"}
    return ctx


def test_governance_action_types_exposed():
    """Enum carries the three governance values added by P1.1."""
    assert ActionType.DOWN_WEIGHT.value == "down_weight"
    assert ActionType.THROTTLE.value == "throttle"
    assert ActionType.VETO.value == "veto"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_subject_prefix"),
    [
        (ActionType.DOWN_WEIGHT, "cio.weight"),
        (ActionType.THROTTLE, "cio.throttle"),
        (ActionType.VETO, "cio.veto"),
    ],
)
async def test_governance_action_publishes_on_dedicated_subject(
    action: ActionType, expected_subject_prefix: str
):
    """Each governance action publishes to cio.<kind>.<strategy_id> with the decision JSON."""
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
    assert subject == f"{expected_subject_prefix}.{strategy_id}"

    payload = json.loads(payload_bytes.decode())
    assert payload["action"] == action.value
    assert payload["justification"] == "governance reasoning"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [ActionType.DOWN_WEIGHT, ActionType.THROTTLE, ActionType.VETO],
)
async def test_governance_action_persists_audit_with_decision_id(action: ActionType):
    """Audit upsert is called for each governance action and carries decision_id."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    context = _make_context("strat_a")
    decision = _make_decision(action)

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(context, decision)

    mock_vc.upsert.assert_called_once()
    kwargs = mock_vc.upsert.call_args.kwargs
    assert kwargs["strategy_id"] == "strat_a"
    payload = kwargs["payload"]
    assert payload["action"] == action.value
    assert payload["decision_id"] == "decision-589"
    assert payload["summary"] == "governance reasoning"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [ActionType.DOWN_WEIGHT, ActionType.THROTTLE, ActionType.VETO],
)
async def test_governance_action_skips_publish_in_dry_run(action: ActionType):
    """DRY_RUN suppresses NATS publish but still persists audit."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    context = _make_context("strat_dry")
    decision = _make_decision(action)

    with patch.dict(os.environ, {"DRY_RUN": "true"}):
        await router.route(context, decision)

    mock_nc.publish.assert_not_called()
    mock_vc.upsert.assert_called_once()
    assert mock_vc.upsert.call_args.kwargs["payload"]["action"] == action.value
