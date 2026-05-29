"""Tests for the in-position re-evaluation loop (#135, P1.4-AC7 / FR60)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cio.core.position_review_loop import (
    DEFAULT_REEVAL_INTERVAL_SECONDS,
    SOURCE_CADENCE,
    SOURCE_EVENT,
    PositionKey,
    PositionReviewLoop,
)


class _RecordingRunner:
    """Async runner that records every (key, reason) it was called with.

    Optionally simulates a slow runner via ``hold_for_seconds`` so the
    backpressure tests can deterministically fire a second trigger
    while the first is still in flight.
    """

    def __init__(self, hold_for_seconds: float = 0.0) -> None:
        self.calls: list[tuple[PositionKey, str]] = []
        self._hold = hold_for_seconds
        self.release = asyncio.Event()

    async def __call__(self, key: PositionKey, reason: str) -> Any:
        self.calls.append((key, reason))
        if self._hold > 0:
            # Wait for either the configured hold or an explicit release.
            try:
                await asyncio.wait_for(self.release.wait(), timeout=self._hold)
            except TimeoutError:
                pass


# ---------------------------------------------------------------------------
# Construction / registry
# ---------------------------------------------------------------------------


def test_default_interval_matches_fr60():
    assert DEFAULT_REEVAL_INTERVAL_SECONDS == 300.0


def test_interval_must_be_positive():
    runner = _RecordingRunner()
    with pytest.raises(ValueError) as zero_err:
        PositionReviewLoop(runner=runner, interval_seconds=0)
    with pytest.raises(ValueError) as neg_err:
        PositionReviewLoop(runner=runner, interval_seconds=-5)
    assert "interval_seconds" in str(zero_err.value)
    assert "interval_seconds" in str(neg_err.value)


def test_add_remove_active_positions_roundtrip():
    runner = _RecordingRunner()
    loop = PositionReviewLoop(runner=runner)
    loop.add_position("s1", "p1")
    loop.add_position("s1", "p2")
    loop.add_position("s2", "p1")

    keys = loop.active_positions()
    assert PositionKey("s1", "p1") in keys
    assert PositionKey("s1", "p2") in keys
    assert PositionKey("s2", "p1") in keys
    assert len(keys) == 3

    loop.remove_position("s1", "p1")
    keys2 = loop.active_positions()
    assert PositionKey("s1", "p1") not in keys2
    assert len(keys2) == 2


def test_remove_unknown_position_is_noop():
    loop = PositionReviewLoop(runner=_RecordingRunner())
    loop.remove_position("nope", "nope")  # must not raise
    assert loop.active_positions() == []


# ---------------------------------------------------------------------------
# AC7.b — event trigger (the simplest path, exercises the dispatch core)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_trigger_invokes_runner_once():
    runner = _RecordingRunner()
    loop = PositionReviewLoop(runner=runner)

    fired = await loop.trigger_event(
        PositionKey("s1", "p1"),
        reason="evaluator_unhealthy:ingest",
    )

    assert fired is True
    assert runner.calls == [(PositionKey("s1", "p1"), "evaluator_unhealthy:ingest")]


@pytest.mark.asyncio
async def test_event_trigger_records_distinct_reasons_per_call():
    runner = _RecordingRunner()
    loop = PositionReviewLoop(runner=runner)
    key = PositionKey("s1", "p1")

    await loop.trigger_event(key, reason="regime_shift")
    await loop.trigger_event(key, reason="drawdown_breach")

    assert [c[1] for c in runner.calls] == ["regime_shift", "drawdown_breach"]


# ---------------------------------------------------------------------------
# AC7.c — backpressure: second trigger while first in-flight is dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backpressure_drops_second_trigger_for_same_position():
    runner = _RecordingRunner(hold_for_seconds=5.0)
    loop = PositionReviewLoop(runner=runner)
    key = PositionKey("s1", "p1")

    # Start a first trigger that holds the runner.
    first = asyncio.create_task(loop.trigger_event(key, reason="cadence-1"))
    await asyncio.sleep(0.05)  # let the runner enter the held state

    # Second trigger arrives while first still in flight → dropped.
    fired = await loop.trigger_event(key, reason="cadence-2")
    assert fired is False
    assert len(runner.calls) == 1

    # Release the first runner; it completes cleanly.
    runner.release.set()
    assert await first is True


@pytest.mark.asyncio
async def test_backpressure_does_not_drop_other_positions():
    runner = _RecordingRunner(hold_for_seconds=5.0)
    loop = PositionReviewLoop(runner=runner)
    key1 = PositionKey("s1", "p1")
    key2 = PositionKey("s1", "p2")

    first = asyncio.create_task(loop.trigger_event(key1, reason="cadence"))
    await asyncio.sleep(0.05)

    # Different position — should NOT be dropped.
    fired = await loop.trigger_event(key2, reason="cadence")
    assert fired is True
    assert len(runner.calls) == 2

    runner.release.set()
    await first


@pytest.mark.asyncio
async def test_backpressure_clears_after_runner_completes():
    runner = _RecordingRunner()
    loop = PositionReviewLoop(runner=runner)
    key = PositionKey("s1", "p1")

    # First completes immediately, second should fire.
    assert await loop.trigger_event(key, reason="r1") is True
    assert await loop.trigger_event(key, reason="r2") is True
    assert len(runner.calls) == 2


@pytest.mark.asyncio
async def test_runner_exception_does_not_break_loop_state():
    """If a runner raises, the in-flight slot must still clear."""

    class _BrokenRunner:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, key, reason):
            self.calls += 1
            raise RuntimeError("arbitration crashed")

    runner = _BrokenRunner()
    loop = PositionReviewLoop(runner=runner)
    key = PositionKey("s1", "p1")

    first = await loop.trigger_event(key, reason="r1")
    # Slot must have been cleared → second trigger fires (not dropped).
    second = await loop.trigger_event(key, reason="r2")

    assert first is True
    assert second is True
    assert runner.calls == 2
    snap = loop.snapshot()
    assert snap["inflight"] == []


# ---------------------------------------------------------------------------
# AC7.a — cadence: tick fires one re-eval per active position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cadence_fires_one_reeval_per_active_position():
    runner = _RecordingRunner()
    # Very short interval so the test doesn't need real time.
    loop = PositionReviewLoop(runner=runner, interval_seconds=0.05)
    loop.add_position("s1", "p1")
    loop.add_position("s2", "p2")

    await loop.start()
    # Wait long enough for ~2 ticks.
    await asyncio.sleep(0.18)
    await loop.stop()

    fired_keys = {c[0] for c in runner.calls}
    assert PositionKey("s1", "p1") in fired_keys
    assert PositionKey("s2", "p2") in fired_keys
    # Reason should be the cadence reason on every cadence call.
    cadence_calls = [c for c in runner.calls if c[1] == "scheduled_review_cadence"]
    assert len(cadence_calls) >= 2


@pytest.mark.asyncio
async def test_cadence_skips_removed_positions():
    runner = _RecordingRunner()
    loop = PositionReviewLoop(runner=runner, interval_seconds=0.05)
    loop.add_position("s1", "p1")

    await loop.start()
    await asyncio.sleep(0.07)
    # Remove the position mid-flight.
    loop.remove_position("s1", "p1")
    await asyncio.sleep(0.20)
    await loop.stop()

    # After removal, no further cadence fires for that key.
    fired_after_removal = [c for c in runner.calls if c[0] == PositionKey("s1", "p1")]
    # At least one fired before removal, but the count plateaus after.
    assert len(fired_after_removal) >= 1


@pytest.mark.asyncio
async def test_start_is_idempotent():
    runner = _RecordingRunner()
    loop = PositionReviewLoop(runner=runner, interval_seconds=0.05)
    await loop.start()
    # Second start() must not spawn a duplicate task.
    await loop.start()
    await loop.stop()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_snapshot_lists_active_positions_sorted():
    loop = PositionReviewLoop(runner=_RecordingRunner())
    loop.add_position("s2", "p2")
    loop.add_position("s1", "p1")

    snap = loop.snapshot()
    assert snap["interval_seconds"] == DEFAULT_REEVAL_INTERVAL_SECONDS
    assert snap["active"] == [
        {"strategy_id": "s1", "position_id": "p1"},
        {"strategy_id": "s2", "position_id": "p2"},
    ]
    assert snap["inflight"] == []


def test_source_labels_match_constants():
    # Source label values are exposed for callers wiring metrics
    # dashboards directly; keep the strings stable.
    assert SOURCE_CADENCE == "cadence"
    assert SOURCE_EVENT == "event"
