"""Tests for the strategy lifecycle state machine (P1.2, #114).

Covers the happy-path traversal of every documented transition and the
per-transition guard rails (invalid (from_state, action) pairs raise).
Every transition is verified to carry a `decision_id` per the P0.1
cross-service identifier contract.
"""

from __future__ import annotations

import pytest

from cio.core.lifecycle import (
    InvalidTransition,
    LifecycleState,
    StrategyAlreadyRegistered,
    StrategyLifecycleStore,
    StrategyNotRegistered,
)
from cio.models.enums import ActionType

SID = "strat_test"
DEF = {"family": "momentum", "version": "1.0.0"}


@pytest.fixture
def store() -> StrategyLifecycleStore:
    return StrategyLifecycleStore()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_creates_registered_state_with_genesis_event(store):
    event = store.register(SID, DEF, decision_id="dec-1", reasoning={"why": "new"})

    assert store.get_state(SID) is LifecycleState.REGISTERED
    assert event.from_state is None
    assert event.action is None  # genesis event has no action
    assert event.to_state is LifecycleState.REGISTERED
    assert event.decision_id == "dec-1"
    assert event.reasoning == {"why": "new"}

    history = store.get_history(SID)
    assert history == [event]


def test_register_mints_decision_id_when_omitted(store):
    event = store.register(SID, DEF)
    assert event.decision_id
    assert len(event.decision_id) >= 16  # uuid4 hex


def test_register_duplicates_rejected(store):
    store.register(SID, DEF)
    with pytest.raises(StrategyAlreadyRegistered) as exc_info:
        store.register(SID, DEF)
    assert SID in str(exc_info.value)


def test_transition_on_unknown_strategy_raises(store):
    with pytest.raises(StrategyNotRegistered) as exc_info:
        store.admit("ghost")
    assert "ghost" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Happy path — full traversal through PROMOTE → DEMOTE → RETIRE
# ---------------------------------------------------------------------------


def test_full_happy_path_registered_through_retired(store):
    store.register(SID, DEF)
    store.characterize(SID)
    store.admit_small(SID, reasoning={"size": "small"})
    store.promote(SID, reasoning={"perf": "exceeds_target"})
    store.demote(SID, reasoning={"perf": "drift"})
    store.promote(SID, reasoning={"perf": "recovered"})  # demoted → graduated
    final = store.retire(SID, reasoning={"reason": "operator_request"})

    assert final.to_state is LifecycleState.RETIRED
    assert store.get_state(SID) is LifecycleState.RETIRED

    history = store.get_history(SID)
    actions = [e.action.value if e.action else None for e in history]
    assert actions == [
        None,  # genesis (REGISTERED)
        None,  # characterize (internal transition)
        ActionType.ADMIT_SMALL.value,
        ActionType.PROMOTE.value,
        ActionType.DEMOTE.value,
        ActionType.PROMOTE.value,
        ActionType.RETIRE.value,
    ]
    # Every event carries a decision_id (P0.1 contract).
    assert all(e.decision_id for e in history)


def test_characterize_to_admit_full_weight(store):
    store.register(SID, DEF)
    store.characterize(SID)
    event = store.admit(SID, reasoning={"size": "full"})
    assert event.to_state is LifecycleState.ADMITTED
    assert event.action is ActionType.ADMIT


def test_characterize_to_reject_terminal(store):
    store.register(SID, DEF)
    store.characterize(SID)
    event = store.reject(SID, reasoning={"why": "backtest_fail"})
    assert event.to_state is LifecycleState.REJECTED
    # Rejected is terminal — any further transition fails.
    with pytest.raises(InvalidTransition):
        store.promote(SID)
    with pytest.raises(InvalidTransition):
        store.retire(SID)


# ---------------------------------------------------------------------------
# Transition guards — invalid (from_state, action) raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "premature_action_name",
    ["admit", "admit_small", "reject", "promote", "demote", "retire"],
)
def test_transitions_from_registered_are_blocked_before_characterize(
    store, premature_action_name
):
    store.register(SID, DEF)
    method = getattr(store, premature_action_name)
    with pytest.raises(InvalidTransition) as exc_info:
        method(SID)
    assert "registered" in str(exc_info.value)


@pytest.mark.parametrize(
    "invalid_after_admit_small",
    ["admit", "admit_small", "reject"],
)
def test_on_trial_blocks_pre_trial_transitions(store, invalid_after_admit_small):
    store.register(SID, DEF)
    store.characterize(SID)
    store.admit_small(SID)
    # From ON_TRIAL only promote / demote are valid.
    method = getattr(store, invalid_after_admit_small)
    with pytest.raises(InvalidTransition) as exc_info:
        method(SID)
    assert "on_trial" in str(exc_info.value)


def test_admitted_can_retire_or_demote_but_not_promote(store):
    store.register(SID, DEF)
    store.characterize(SID)
    store.admit(SID)
    # Promote is not valid from Admitted (full weight already).
    with pytest.raises(InvalidTransition):
        store.promote(SID)
    # Demote and retire are valid from Admitted.
    store.demote(SID)
    assert store.get_state(SID) is LifecycleState.DEMOTED


def test_retired_is_terminal(store):
    store.register(SID, DEF)
    store.characterize(SID)
    store.admit_small(SID)
    store.promote(SID)
    store.retire(SID)
    blocked_methods = ("admit", "admit_small", "promote", "demote", "retire")
    for method_name in blocked_methods:
        method = getattr(store, method_name)
        with pytest.raises(InvalidTransition) as exc_info:
            method(SID)
        assert "retired" in str(exc_info.value)
    assert store.get_state(SID) is LifecycleState.RETIRED


# ---------------------------------------------------------------------------
# Read API surface
# ---------------------------------------------------------------------------


def test_list_strategies_and_definition_access(store):
    store.register("a", {"family": "mean_rev"})
    store.register("b", {"family": "momentum"})
    assert set(store.list_strategies()) == {"a", "b"}
    assert store.get_definition("a") == {"family": "mean_rev"}


def test_history_is_defensive_copy(store):
    store.register(SID, DEF)
    history = store.get_history(SID)
    history.clear()
    # The store's internal history must be untouched.
    assert len(store.get_history(SID)) == 1
