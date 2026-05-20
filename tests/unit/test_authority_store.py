"""Tests for the per-action authority store (P1.3, #115).

Covers the three authority states (ENABLED / OPERATOR_APPROVAL_REQUIRED /
DISABLED), the audit log with operator identity, the pending-approval
queue, and the default-fallback table.
"""

from __future__ import annotations

import pytest

from cio.core.authority import (
    DEFAULT_FALLBACKS,
    ActionAuthority,
    AuthorityStore,
    apply_authority,
)
from cio.models.enums import ActionType


@pytest.fixture
def store() -> AuthorityStore:
    return AuthorityStore()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_state_is_enabled_for_every_action_type(store):
    states = store.get_all()
    assert all(s is ActionAuthority.ENABLED for s in states.values())
    assert set(states.keys()) == set(ActionType)


def test_default_fallback_table_covers_dispatch_actions():
    """Every action that produces a NATS dispatch has a safer fallback."""
    dispatch_actions = {
        ActionType.EXECUTE,
        ActionType.MODIFY_PARAMS,
        ActionType.PAUSE_STRATEGY,
        ActionType.ESCALATE,
        ActionType.RETRY_SAFE,
        ActionType.FAIL_SAFE,
        ActionType.DOWN_WEIGHT,
        ActionType.THROTTLE,
        ActionType.VETO,
        ActionType.ADMIT,
        ActionType.ADMIT_SMALL,
        ActionType.PROMOTE,
        ActionType.DEMOTE,
        ActionType.RETIRE,
    }
    safe_terminals = {ActionType.SKIP, ActionType.BLOCK, ActionType.REJECT}
    for action in dispatch_actions:
        assert action in DEFAULT_FALLBACKS, action
        assert DEFAULT_FALLBACKS[action] in safe_terminals, (
            action,
            DEFAULT_FALLBACKS[action],
        )


# ---------------------------------------------------------------------------
# CRUD + audit
# ---------------------------------------------------------------------------


def test_set_state_records_audit_change(store):
    change = store.set_state(
        ActionType.EXECUTE,
        ActionAuthority.OPERATOR_APPROVAL_REQUIRED,
        operator_id="op-yuri",
        reason="manual review week",
    )
    assert change.from_state is ActionAuthority.ENABLED
    assert change.to_state is ActionAuthority.OPERATOR_APPROVAL_REQUIRED
    assert change.operator_id == "op-yuri"
    assert change.reason == "manual review week"

    audit = store.get_audit()
    assert audit == [change]
    assert (
        store.get_state(ActionType.EXECUTE)
        is ActionAuthority.OPERATOR_APPROVAL_REQUIRED
    )


def test_set_state_rejects_empty_operator(store):
    with pytest.raises(ValueError) as exc_info:
        store.set_state(
            ActionType.EXECUTE,
            ActionAuthority.DISABLED,
            operator_id="",
            reason="who cares",
        )
    assert "operator_id" in str(exc_info.value)


def test_audit_log_preserves_full_history(store):
    store.set_state(
        ActionType.EXECUTE,
        ActionAuthority.OPERATOR_APPROVAL_REQUIRED,
        operator_id="op-a",
        reason="step 1",
    )
    store.set_state(
        ActionType.EXECUTE,
        ActionAuthority.ENABLED,
        operator_id="op-b",
        reason="step 2",
    )
    audit = store.get_audit()
    assert len(audit) == 2
    assert audit[0].operator_id == "op-a"
    assert audit[1].operator_id == "op-b"
    assert audit[0].to_state is ActionAuthority.OPERATOR_APPROVAL_REQUIRED
    assert audit[1].to_state is ActionAuthority.ENABLED


def test_audit_is_defensive_copy(store):
    store.set_state(
        ActionType.EXECUTE,
        ActionAuthority.DISABLED,
        operator_id="op-a",
        reason="r",
    )
    audit = store.get_audit()
    audit.clear()
    assert len(store.get_audit()) == 1


# ---------------------------------------------------------------------------
# Pending-approval queue
# ---------------------------------------------------------------------------


def _enqueue(store: AuthorityStore):
    return store.enqueue_pending(
        action=ActionType.EXECUTE,
        strategy_id="strat_alpha",
        decision_id="dec-115-1",
        correlation_id="corr-115-1",
        context_payload={"symbol": "BTCUSDT", "trigger": "intent"},
        decision_payload={"action": "execute", "justification": "j"},
    )


def test_enqueue_pending_returns_queue_entry(store):
    pending = _enqueue(store)
    assert pending.queue_id
    assert pending.action is ActionType.EXECUTE
    assert pending.decision_id == "dec-115-1"
    assert pending.context_payload["symbol"] == "BTCUSDT"

    listing = store.list_pending()
    assert listing == [pending]


def test_approve_pending_removes_from_queue(store):
    pending = _enqueue(store)
    resolution = store.approve_pending(
        pending.queue_id, operator_id="op-yuri", reason="trade looks fine"
    )
    assert resolution.approved is True
    assert resolution.pending == pending
    assert resolution.operator_id == "op-yuri"
    assert store.list_pending() == []


def test_reject_pending_removes_from_queue(store):
    pending = _enqueue(store)
    resolution = store.reject_pending(
        pending.queue_id, operator_id="op-yuri", reason="too risky"
    )
    assert resolution.approved is False
    assert resolution.pending == pending
    assert store.list_pending() == []


def test_approve_unknown_queue_id_raises(store):
    with pytest.raises(KeyError) as exc_info:
        store.approve_pending("ghost-id", operator_id="op-a")
    assert "ghost-id" in str(exc_info.value)


def test_resolve_pending_requires_operator(store):
    pending = _enqueue(store)
    with pytest.raises(ValueError) as exc_info:
        store.approve_pending(pending.queue_id, operator_id="")
    assert "operator_id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# apply_authority decision helper
# ---------------------------------------------------------------------------


def _apply(store, action: ActionType):
    return apply_authority(
        store,
        action=action,
        strategy_id="strat_alpha",
        decision_id="dec-115-x",
        correlation_id="corr-115-x",
        context_payload={"symbol": "BTCUSDT"},
        decision_payload={"action": action.value},
    )


def test_apply_authority_enabled_passes_through(store):
    decision = _apply(store, ActionType.EXECUTE)
    assert decision.pending is None
    assert decision.was_disabled is False
    assert decision.action is ActionType.EXECUTE
    assert decision.original is ActionType.EXECUTE


def test_apply_authority_approval_required_enqueues(store):
    store.set_state(
        ActionType.EXECUTE,
        ActionAuthority.OPERATOR_APPROVAL_REQUIRED,
        operator_id="op-a",
        reason="review week",
    )
    decision = _apply(store, ActionType.EXECUTE)
    assert decision.pending is not None
    assert decision.pending.action is ActionType.EXECUTE
    assert decision.was_disabled is False
    assert decision.original is ActionType.EXECUTE
    assert store.list_pending() == [decision.pending]


@pytest.mark.parametrize(
    ("action", "expected_fallback"),
    [
        (ActionType.EXECUTE, ActionType.SKIP),
        (ActionType.MODIFY_PARAMS, ActionType.SKIP),
        (ActionType.PAUSE_STRATEGY, ActionType.BLOCK),
        (ActionType.ADMIT, ActionType.REJECT),
        (ActionType.PROMOTE, ActionType.SKIP),
        (ActionType.RETIRE, ActionType.SKIP),
        (ActionType.VETO, ActionType.BLOCK),
    ],
)
def test_apply_authority_disabled_substitutes_fallback(
    store, action, expected_fallback
):
    store.set_state(
        action,
        ActionAuthority.DISABLED,
        operator_id="op-a",
        reason="off-policy",
    )
    decision = _apply(store, action)
    assert decision.pending is None
    assert decision.was_disabled is True
    assert decision.action is expected_fallback
    assert decision.original is action


def test_disabled_unknown_action_falls_back_to_skip(store):
    # Override DEFAULT_FALLBACKS to simulate an action without a mapping —
    # the store should still produce a safe SKIP.
    custom = AuthorityStore(fallbacks={})
    custom.set_state(
        ActionType.EXECUTE,
        ActionAuthority.DISABLED,
        operator_id="op-a",
        reason="r",
    )
    decision = apply_authority(
        custom,
        action=ActionType.EXECUTE,
        strategy_id="s",
        decision_id="d",
        correlation_id="c",
        context_payload={},
        decision_payload={"action": "execute"},
    )
    assert decision.was_disabled is True
    assert decision.action is ActionType.SKIP
