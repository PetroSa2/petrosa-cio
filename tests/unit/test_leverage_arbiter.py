"""Tests for the admission-time leverage arbiter (#137, FR61 / AC3.b + AC3.d).

Covers the three AC3.b branches (accept / override / fallback) and the
AC3.a env-var fallback. Pure-function tests — no DB, no NATS.
"""

from __future__ import annotations

import pytest

from cio.core.leverage_arbiter import (
    DEFAULT_OPERATOR_MAX_LEVERAGE,
    LeverageDecision,
    arbitrate_leverage,
    operator_max_from_env,
)

# ---------------------------------------------------------------------------
# AC3.a — env-var fallback


def test_operator_max_from_env_default_when_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CIO_DEFAULT_MAX_LEVERAGE", raising=False)
    assert operator_max_from_env() == DEFAULT_OPERATOR_MAX_LEVERAGE


def test_operator_max_from_env_respects_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CIO_DEFAULT_MAX_LEVERAGE", "25")
    assert operator_max_from_env() == 25


def test_operator_max_from_env_falls_back_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CIO_DEFAULT_MAX_LEVERAGE", "not-a-number")
    assert operator_max_from_env() == DEFAULT_OPERATOR_MAX_LEVERAGE


def test_operator_max_from_env_clamps_below_one(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CIO_DEFAULT_MAX_LEVERAGE", "0")
    assert operator_max_from_env() == 1


# ---------------------------------------------------------------------------
# AC3.b — arbitration branches


def test_accept_branch_when_recommendation_within_bound():
    decision = arbitrate_leverage(
        recommended_leverage=5,
        operator_max=10,
        strategy_envelope=None,
    )
    assert isinstance(decision, LeverageDecision)
    assert decision.decided_leverage == 5
    assert decision.per_strategy_bound == 10
    assert decision.branch == "accept"
    assert "accepted as-is" in decision.audit_reason


def test_accept_branch_at_exact_bound():
    decision = arbitrate_leverage(
        recommended_leverage=10,
        operator_max=10,
    )
    assert decision.decided_leverage == 10
    assert decision.branch == "accept"


def test_override_branch_clamps_at_bound_without_rejecting():
    decision = arbitrate_leverage(
        recommended_leverage=25,
        operator_max=10,
    )
    assert decision.decided_leverage == 10
    assert decision.branch == "override"
    assert "overridden" in decision.audit_reason
    # Defensive — AC3.b explicitly says "do not reject".
    assert decision.decided_leverage == decision.per_strategy_bound


def test_fallback_branch_uses_bound_when_recommendation_absent():
    decision = arbitrate_leverage(
        recommended_leverage=None,
        operator_max=10,
    )
    assert decision.decided_leverage == 10
    assert decision.branch == "fallback"
    assert "no recommended_leverage" in decision.audit_reason


def test_strategy_envelope_tightens_bound_below_operator_max():
    decision = arbitrate_leverage(
        recommended_leverage=8,
        operator_max=10,
        strategy_envelope=5,
    )
    assert decision.per_strategy_bound == 5  # min(10, 5)
    assert decision.decided_leverage == 5  # 8 > 5, override
    assert decision.branch == "override"


def test_strategy_envelope_with_no_recommendation_uses_min_of_two_bounds():
    decision = arbitrate_leverage(
        recommended_leverage=None,
        operator_max=10,
        strategy_envelope=4,
    )
    assert decision.decided_leverage == 4
    assert decision.per_strategy_bound == 4
    assert decision.branch == "fallback"


def test_strategy_envelope_above_operator_max_is_ignored():
    decision = arbitrate_leverage(
        recommended_leverage=20,
        operator_max=10,
        strategy_envelope=50,
    )
    assert decision.per_strategy_bound == 10  # min(10, 50)
    assert decision.decided_leverage == 10
    assert decision.branch == "override"


# ---------------------------------------------------------------------------
# Defensive paths


def test_zero_recommendation_treated_as_missing():
    decision = arbitrate_leverage(
        recommended_leverage=0,
        operator_max=10,
    )
    assert decision.branch == "fallback"
    assert decision.decided_leverage == 10
    assert "below 1" in decision.audit_reason


def test_negative_recommendation_treated_as_missing():
    decision = arbitrate_leverage(
        recommended_leverage=-3,
        operator_max=10,
    )
    assert decision.branch == "fallback"
    assert decision.decided_leverage == 10


def test_operator_max_below_one_is_clamped():
    decision = arbitrate_leverage(
        recommended_leverage=5,
        operator_max=0,
    )
    assert decision.per_strategy_bound == 1
    assert decision.decided_leverage == 1
    assert decision.branch == "override"


def test_no_explicit_operator_max_picks_up_env(monkeypatch: pytest.MonkeyPatch):
    """AC3.a fallback path: when caller omits operator_max we read the env."""
    monkeypatch.setenv("CIO_DEFAULT_MAX_LEVERAGE", "7")
    decision = arbitrate_leverage(recommended_leverage=20)
    assert decision.per_strategy_bound == 7
    assert decision.decided_leverage == 7
    assert decision.branch == "override"


def test_default_when_env_unset_uses_module_default(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("CIO_DEFAULT_MAX_LEVERAGE", raising=False)
    decision = arbitrate_leverage(recommended_leverage=None)
    assert decision.decided_leverage == DEFAULT_OPERATOR_MAX_LEVERAGE


def test_decision_is_immutable():
    """LeverageDecision is a frozen dataclass — mutation raises FrozenInstanceError."""
    import dataclasses

    decision = arbitrate_leverage(recommended_leverage=5, operator_max=10)
    with pytest.raises(dataclasses.FrozenInstanceError) as excinfo:
        decision.decided_leverage = 99  # type: ignore[misc]
    assert "decided_leverage" in str(excinfo.value)
