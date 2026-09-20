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

    # Governance dispatch + decision audit copy (#610 P7.1). VETO also
    # emits the FR66 alert (#139, P8-AC2b) so its call count is 3; the
    # non-alert governance actions stay at 2.
    is_fr66_alert_action = action.value.lower() in {
        "veto",
        "demote",
        "retire",
        "exit_now",
    }
    expected_count = 3 if is_fr66_alert_action else 2
    assert mock_nc.publish.call_count == expected_count
    calls = {c.args[0]: c.args[1] for c in mock_nc.publish.call_args_list}
    governance_subject = f"{expected_subject_prefix}.{strategy_id}"
    audit_subject = f"cio.decision.audit.{action.value}"
    assert governance_subject in calls
    assert audit_subject in calls
    if is_fr66_alert_action:
        fr66_subject = f"alerts.cio.{action.value.lower()}.{strategy_id}"
        assert fr66_subject in calls
        fr66_payload = json.loads(calls[fr66_subject].decode())
        assert fr66_payload["category"] == "cio_governance_action"
        assert fr66_payload["severity"] == "critical"

    payload = json.loads(calls[governance_subject].decode())
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        ActionType.RETRY_SAFE,
        ActionType.ESCALATE,
        ActionType.DOWN_WEIGHT,
        ActionType.THROTTLE,
        ActionType.VETO,
        ActionType.FAIL_SAFE,
    ],
)
async def test_router_sanitizes_spaced_strategy_id_in_nats_subjects(
    action: ActionType,
):
    """petrosa-cio#211 regression: a producer-supplied display-name
    strategy_id containing spaces (e.g. "Iceberg Order Detector  625") must
    never reach a published NATS subject. NATS subjects cannot contain
    whitespace — the server's processPub parser splits on it and rejects
    the PUB (silently dropping cio.retry.*, signals.trading.*, etc.).
    """
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    context = _make_context("Iceberg Order Detector  625")
    decision = _make_decision(action)

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(context, decision)

    assert mock_nc.publish.await_count >= 1
    for call in mock_nc.publish.call_args_list:
        subject = call.args[0]
        assert " " not in subject, f"subject contains whitespace: {subject!r}"
        assert subject == subject.strip()

    published_subjects = {c.args[0] for c in mock_nc.publish.call_args_list}
    assert any(s.endswith("iceberg_order_detector_625") for s in published_subjects), (
        published_subjects
    )


@pytest.mark.asyncio
async def test_router_sanitizes_spaced_strategy_id_for_execute_action():
    """EXECUTE fans out to both signals.trading.<id> (legacy) and
    trade.execute.<id> (modern) — both must be whitespace-free (#211)."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    context = _make_context("Spread Liquidity Monitor  625")
    context.trigger_payload = {"symbol": "BTCUSDT"}
    decision = _make_decision(ActionType.EXECUTE)

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(context, decision)

    published_subjects = {c.args[0] for c in mock_nc.publish.call_args_list}
    for subject in published_subjects:
        assert " " not in subject, f"subject contains whitespace: {subject!r}"

    assert "trade.execute.spread_liquidity_monitor_625" in published_subjects
