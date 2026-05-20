"""Tests for OutputRouter ↔ AuthorityStore integration (P1.3, #115).

Covers the three runtime outcomes a configured authority store can produce
at dispatch time:

  * ENABLED                       → original behavior, action dispatched as-is
  * OPERATOR_APPROVAL_REQUIRED    → decision diverted to pending queue, no
                                    NATS publish, audit-trail records the
                                    diversion with the queue_id
  * DISABLED                      → action substituted with the fallback,
                                    fallback is dispatched normally (or
                                    SKIP-style no-publish), audit-trail
                                    records the original action

`decision_id` propagation is verified end-to-end (P0.1 contract).
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.authority import ActionAuthority, AuthorityStore
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
        justification="authority test",
        thought_trace="audit trace",
    )


def _make_context(strategy_id: str = "strat_a") -> TriggerContext:
    ctx = MagicMock(spec=TriggerContext)
    ctx.strategy_id = strategy_id
    ctx.decision_id = "decision-115"
    ctx.correlation_id = "corr-115"
    ctx.trigger_payload = {"symbol": "BTCUSDT"}
    return ctx


# ---------------------------------------------------------------------------
# ENABLED (default) — passes through unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enabled_action_dispatches_normally_via_authority_store():
    store = AuthorityStore()  # everything ENABLED by default
    router = OutputRouter(
        nats_client=AsyncMock(),
        vector_client=AsyncMock(),
        ta_bot_url="http://ta-bot",
        authority_store=store,
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(_make_context(), _make_decision(ActionType.ADMIT))

    # ADMIT (a lifecycle action) publishes on cio.lifecycle.admit.<sid>.
    router.nats_client.publish.assert_called_once()
    subject, _payload = router.nats_client.publish.call_args.args
    assert subject == "cio.lifecycle.admit.strat_a"
    # No pending-approval entries because action was ENABLED.
    assert store.list_pending() == []


# ---------------------------------------------------------------------------
# OPERATOR_APPROVAL_REQUIRED — divert to pending queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_required_diverts_and_skips_dispatch():
    store = AuthorityStore()
    store.set_state(
        ActionType.EXECUTE,
        ActionAuthority.OPERATOR_APPROVAL_REQUIRED,
        operator_id="op-yuri",
        reason="review week",
    )
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        authority_store=store,
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(_make_context(), _make_decision(ActionType.EXECUTE))

    # NATS dispatch must NOT have happened.
    mock_nc.publish.assert_not_called()

    # Pending queue should hold exactly one entry for this decision.
    pending = store.list_pending()
    assert len(pending) == 1
    assert pending[0].action is ActionType.EXECUTE
    assert pending[0].decision_id == "decision-115"
    assert pending[0].strategy_id == "strat_a"

    # Audit trail records the diversion (event_type=decision_pending_approval).
    audit_calls = [c for c in mock_vc.upsert.call_args_list]
    assert len(audit_calls) == 1
    audit_payload = audit_calls[0].kwargs["payload"]
    assert audit_payload["event_type"] == "decision_pending_approval"
    assert audit_payload["action"] == "execute"
    assert audit_payload["decision_id"] == "decision-115"
    assert audit_payload["queue_id"] == pending[0].queue_id


# ---------------------------------------------------------------------------
# DISABLED — substitute with next-best safe action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_action_substitutes_fallback_and_records_original():
    """EXECUTE → SKIP fallback when EXECUTE is DISABLED.

    SKIP is a no-publish action, so the router must not call publish for the
    substituted action. The audit trail records the SKIP plus the
    `authority_fallback_from` field pointing at the original EXECUTE.
    """
    store = AuthorityStore()
    store.set_state(
        ActionType.EXECUTE,
        ActionAuthority.DISABLED,
        operator_id="op-yuri",
        reason="off-policy",
    )
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        authority_store=store,
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(_make_context(), _make_decision(ActionType.EXECUTE))

    # EXECUTE → SKIP fallback. SKIP does not publish.
    mock_nc.publish.assert_not_called()

    mock_vc.upsert.assert_called_once()
    payload = mock_vc.upsert.call_args.kwargs["payload"]
    assert payload["action"] == "skip"
    assert payload["authority_fallback_from"] == "execute"
    assert payload["decision_id"] == "decision-115"


@pytest.mark.asyncio
async def test_disabled_lifecycle_action_dispatches_safe_fallback():
    """ADMIT → REJECT fallback when ADMIT is DISABLED.

    REJECT is itself a lifecycle action that publishes — the router must
    dispatch the fallback, not the original.
    """
    store = AuthorityStore()
    store.set_state(
        ActionType.ADMIT,
        ActionAuthority.DISABLED,
        operator_id="op-yuri",
        reason="freeze admissions",
    )
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        authority_store=store,
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(_make_context(), _make_decision(ActionType.ADMIT))

    # Fallback publishes on cio.lifecycle.reject.<sid>, NOT cio.lifecycle.admit.<sid>.
    mock_nc.publish.assert_called_once()
    subject, payload_bytes = mock_nc.publish.call_args.args
    assert subject == "cio.lifecycle.reject.strat_a"

    payload = json.loads(payload_bytes.decode())
    # The decision payload's `action` field is the original — we do not mutate
    # the DecisionResult on the wire; only the dispatch subject reflects the
    # fallback. (Audit-trail captures the swap via authority_fallback_from.)
    assert payload["action"] == "admit"

    audit_payload = mock_vc.upsert.call_args.kwargs["payload"]
    assert audit_payload["action"] == "reject"
    assert audit_payload["authority_fallback_from"] == "admit"


# ---------------------------------------------------------------------------
# Backwards compatibility — no authority_store wired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_without_authority_store_preserves_behavior():
    """authority_store=None → no behavioral change from pre-P1.3 routing."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(_make_context(), _make_decision(ActionType.ADMIT))

    mock_nc.publish.assert_called_once()
    subject, _payload = mock_nc.publish.call_args.args
    assert subject == "cio.lifecycle.admit.strat_a"


# ---------------------------------------------------------------------------
# decision_id propagation across all three outcomes (P0.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expect_publish", "expect_pending"),
    [
        (ActionAuthority.ENABLED, True, False),
        (ActionAuthority.OPERATOR_APPROVAL_REQUIRED, False, True),
        (ActionAuthority.DISABLED, False, False),  # EXECUTE → SKIP no-publish
    ],
)
async def test_decision_id_propagates_in_every_outcome(
    state, expect_publish, expect_pending
):
    store = AuthorityStore()
    if state is not ActionAuthority.ENABLED:
        store.set_state(ActionType.EXECUTE, state, operator_id="op-yuri", reason="r")
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        authority_store=store,
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(_make_context(), _make_decision(ActionType.EXECUTE))

    if expect_publish:
        assert mock_nc.publish.called
    else:
        mock_nc.publish.assert_not_called()

    if expect_pending:
        pending = store.list_pending()
        assert pending and pending[0].decision_id == "decision-115"

    # Audit trail always carries decision_id.
    mock_vc.upsert.assert_called_once()
    audit = mock_vc.upsert.call_args.kwargs["payload"]
    assert audit["decision_id"] == "decision-115"
