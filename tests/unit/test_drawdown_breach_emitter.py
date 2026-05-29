"""Tests for P8-AC2c (#140) — drawdown-envelope breach alert producer."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from cio.core.alerting.drawdown_breach_emitter import DrawdownBreachEmitter
from cio.core.alerting.fr66_alerts import (
    CATEGORY_DRAWDOWN_ENVELOPE_BREACH,
    SEVERITY_CRITICAL,
    build_drawdown_breach_alert,
    drawdown_breach_subject,
)


class _RecordingNATS:
    """Captures (subject, payload) tuples without needing nats-py."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload.decode())))


# ---------------------------------------------------------------------------
# AC2.c.2 — payload / subject contract
# ---------------------------------------------------------------------------


def test_drawdown_breach_subject_uses_strategy_id():
    assert (
        drawdown_breach_subject("momentum-v3") == "alerts.drawdown.breach.momentum-v3"
    )


def test_drawdown_breach_subject_handles_empty_strategy_id():
    assert drawdown_breach_subject("") == "alerts.drawdown.breach.unknown"
    assert drawdown_breach_subject("   ") == "alerts.drawdown.breach.unknown"


def test_build_drawdown_breach_alert_payload_shape():
    observed_at = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
    payload = build_drawdown_breach_alert(
        strategy_id="momentum-v3",
        position_id="POS-1234",
        observed_drawdown=0.0712,
        envelope_p99=0.06,
        envelope_p100=0.082,
        observed_at=observed_at,
    )

    assert payload["category"] == CATEGORY_DRAWDOWN_ENVELOPE_BREACH
    assert payload["severity"] == SEVERITY_CRITICAL
    assert payload["strategy_id"] == "momentum-v3"
    assert payload["position_id"] == "POS-1234"
    assert payload["observed_drawdown"] == pytest.approx(0.0712)
    assert payload["envelope_p99"] == pytest.approx(0.06)
    assert payload["envelope_p100"] == pytest.approx(0.082)
    assert payload["timestamp"] == "2026-05-28T12:00:00Z"
    assert payload["dedupe_key"]  # non-empty stable string


def test_build_drawdown_breach_alert_allows_missing_p100():
    payload = build_drawdown_breach_alert(
        strategy_id="s1",
        position_id="p1",
        observed_drawdown=0.1,
        envelope_p99=0.05,
        envelope_p100=None,
    )
    assert payload["envelope_p100"] is None


# ---------------------------------------------------------------------------
# AC2.c.1 — breach detection: fires when realized > p99
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_fires_on_breach_above_p99():
    nats = _RecordingNATS()
    emitter = DrawdownBreachEmitter(nats_client=nats)

    fired = await emitter.check_and_emit(
        strategy_id="momentum-v3",
        position_id="POS-1",
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )

    assert fired is True
    assert len(nats.published) == 1
    subject, payload = nats.published[0]
    assert subject == "alerts.drawdown.breach.momentum-v3"
    assert payload["observed_drawdown"] == pytest.approx(0.07)
    assert payload["envelope_p99"] == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_emit_silent_when_within_envelope():
    nats = _RecordingNATS()
    emitter = DrawdownBreachEmitter(nats_client=nats)

    fired = await emitter.check_and_emit(
        strategy_id="momentum-v3",
        position_id="POS-1",
        realized_drawdown_pct=0.05,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )

    assert fired is False
    assert nats.published == []


@pytest.mark.asyncio
async def test_emit_silent_when_exactly_at_p99():
    """Spec is `>` not `>=` — exactly at p99 is not a breach."""
    nats = _RecordingNATS()
    emitter = DrawdownBreachEmitter(nats_client=nats)

    fired = await emitter.check_and_emit(
        strategy_id="s",
        position_id="p",
        realized_drawdown_pct=0.06,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )

    assert fired is False
    assert nats.published == []


# ---------------------------------------------------------------------------
# AC2.c.3 — dedup window: same (strategy_id, position_id) suppressed
# until the position exits and a new one opens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedupe_suppresses_repeat_breach_same_position():
    nats = _RecordingNATS()
    emitter = DrawdownBreachEmitter(nats_client=nats)

    first = await emitter.check_and_emit(
        strategy_id="s",
        position_id="p",
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )
    second = await emitter.check_and_emit(
        strategy_id="s",
        position_id="p",
        realized_drawdown_pct=0.09,  # even worse — still suppressed
        envelope_p99=0.06,
        envelope_p100=0.082,
    )

    assert first is True
    assert second is False
    assert len(nats.published) == 1


@pytest.mark.asyncio
async def test_dedupe_does_not_suppress_other_positions_same_strategy():
    nats = _RecordingNATS()
    emitter = DrawdownBreachEmitter(nats_client=nats)

    await emitter.check_and_emit(
        strategy_id="s",
        position_id="p1",
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )
    fired_p2 = await emitter.check_and_emit(
        strategy_id="s",
        position_id="p2",  # different position
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )

    assert fired_p2 is True
    assert len(nats.published) == 2


@pytest.mark.asyncio
async def test_dedupe_does_not_suppress_other_strategies_same_position_id():
    """Different strategies happen to have the same position_id namespace
    rarely, but the dedup key MUST be the (strategy_id, position_id) pair,
    not position_id alone — otherwise a breach on strategy B is lost."""
    nats = _RecordingNATS()
    emitter = DrawdownBreachEmitter(nats_client=nats)

    await emitter.check_and_emit(
        strategy_id="s1",
        position_id="POS-shared",
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )
    fired = await emitter.check_and_emit(
        strategy_id="s2",
        position_id="POS-shared",
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )

    assert fired is True
    assert len(nats.published) == 2


@pytest.mark.asyncio
async def test_dedupe_resets_after_position_close_then_reopen():
    nats = _RecordingNATS()
    emitter = DrawdownBreachEmitter(nats_client=nats)

    # First breach for (s, p) fires.
    await emitter.check_and_emit(
        strategy_id="s",
        position_id="p",
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )
    # Suppressed while position is still open.
    await emitter.check_and_emit(
        strategy_id="s",
        position_id="p",
        realized_drawdown_pct=0.09,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )
    # Position exits.
    emitter.notify_position_closed("s", "p")
    # A "new position" (same logical id, re-opened after exit) can re-fire.
    fired_again = await emitter.check_and_emit(
        strategy_id="s",
        position_id="p",
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )

    assert fired_again is True
    assert len(nats.published) == 2


def test_notify_position_closed_is_tolerant_of_missing_keys():
    """No-op on unknown (strategy_id, position_id) is acceptable hygiene."""
    emitter = DrawdownBreachEmitter()
    # Must not raise on an unknown key — and dedup state stays empty.
    emitter.notify_position_closed("nope", "nope")
    assert emitter.fired_keys() == []


# ---------------------------------------------------------------------------
# Best-effort NATS — emit succeeds even without a wired client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_with_no_nats_client_still_records_dedupe():
    """Even when NATS is absent, the dedup record must lock — otherwise a
    transient NATS outage would re-fire the same breach every tick."""
    emitter = DrawdownBreachEmitter(nats_client=None)

    first = await emitter.check_and_emit(
        strategy_id="s",
        position_id="p",
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )
    second = await emitter.check_and_emit(
        strategy_id="s",
        position_id="p",
        realized_drawdown_pct=0.08,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )

    assert first is True
    assert second is False
    assert emitter.fired_keys() == [("s", "p")]


@pytest.mark.asyncio
async def test_emit_continues_when_nats_publish_raises():
    """Producer must not break when the observability bus hiccups."""

    class _BrokenNATS:
        async def publish(self, subject, payload):
            raise RuntimeError("nats down")

    emitter = DrawdownBreachEmitter(nats_client=_BrokenNATS())
    fired = await emitter.check_and_emit(
        strategy_id="s",
        position_id="p",
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )
    # Best-effort: emitter reports it attempted an emit AND records the
    # dedup so the next tick doesn't re-fire.
    assert fired is True
    assert emitter.fired_keys() == [("s", "p")]


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_lists_active_dedupe_records():
    emitter = DrawdownBreachEmitter()
    await emitter.check_and_emit(
        strategy_id="s",
        position_id="p1",
        realized_drawdown_pct=0.07,
        envelope_p99=0.06,
        envelope_p100=0.082,
    )
    snap = emitter.snapshot()
    assert len(snap["fired"]) == 1
    row = snap["fired"][0]
    assert row["strategy_id"] == "s"
    assert row["position_id"] == "p1"
    assert row["observed_drawdown"] == pytest.approx(0.07)
    assert row["envelope_p99"] == pytest.approx(0.06)
    assert "fired_at" in row
