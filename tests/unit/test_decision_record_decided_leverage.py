"""DecisionRecord must carry ``decided_leverage`` (P1.5-AC3.c / #137).

This is the persistence-contract test for the new field — it asserts the
dataclass shape and the legacy/default behavior. The router-wiring side
effect (that every admission decision records a value) is covered by the
broader router test suite when 691.1's signal field lands and the
call-site begins passing non-``None`` recommendations.
"""

from __future__ import annotations

from cio.core.decision_store import DecisionRecord


def test_decided_leverage_defaults_to_none_for_legacy_records():
    """Legacy and pre-EPIC-#691 records have no leverage attached."""
    record = DecisionRecord(
        strategy_id="s1",
        action="execute",
        reasoning_trace="hand-built",
        confidence=0.9,
    )
    assert record.decided_leverage is None


def test_decided_leverage_round_trips_when_set():
    record = DecisionRecord(
        strategy_id="s1",
        action="execute",
        reasoning_trace="hand-built",
        confidence=0.9,
        decided_leverage=7,
    )
    assert record.decided_leverage == 7


def test_decided_leverage_can_carry_arbiter_output():
    """The router wires `arbitrate_leverage(...).decided_leverage` into here."""
    from cio.core.leverage_arbiter import arbitrate_leverage

    decision = arbitrate_leverage(
        recommended_leverage=20,
        operator_max=5,
    )
    record = DecisionRecord(
        strategy_id="s1",
        action="execute",
        reasoning_trace="t",
        confidence=0.5,
        decided_leverage=decision.decided_leverage,
    )
    assert record.decided_leverage == 5  # clamped by override branch
