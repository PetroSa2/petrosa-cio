"""Tests for the envelope-drift alert emitter (#152, P4.6-AC5 / FR62 / FR66).

Coverage maps to ACs:

* AC5.a — divergence detection with configurable threshold; below-threshold
  is silent; above-threshold fires; missing approved envelope is treated as
  divergence = infinity.
* AC5.b — payload + subject shape contract via the
  ``alerts.envelope.drift_detected.<strategy_key>`` subject.
* AC5.c — rate-limit suppression per ``strategy_key``; window-expiry
  re-arms the emitter.
* AC5.d — non-mutation property: the emitter exposes no envelope-update
  method; only inspection + alert publishing.
* AC5.e — the three required branches are explicit tests below
  (``no_approved`` / ``approved_below_threshold`` / ``approved_above_threshold``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from cio.core.alerting.envelope_drift_emitter import (
    DEFAULT_DIVERGENCE_THRESHOLD,
    DEFAULT_RATE_LIMIT_WINDOW,
    EnvelopeDriftEmitter,
    compute_max_divergence_pct,
)
from cio.core.alerting.fr66_alerts import (
    CATEGORY_ENVELOPE_DRIFT_DETECTED,
    SEVERITY_WARNING,
    build_envelope_drift_alert,
    envelope_drift_subject,
)


class _RecordingNATS:
    """Captures (subject, payload) tuples without needing nats-py."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload.decode())))


# ─── AC5.b — subject / payload contract ─────────────────────────────────────


def test_envelope_drift_subject_shape() -> None:
    assert (
        envelope_drift_subject("strategy:btc")
        == "alerts.envelope.drift_detected.strategy:btc"
    )


def test_envelope_drift_subject_handles_empty_key() -> None:
    assert envelope_drift_subject("") == "alerts.envelope.drift_detected.unknown"
    assert envelope_drift_subject("   ") == "alerts.envelope.drift_detected.unknown"


def test_build_envelope_drift_alert_with_prior() -> None:
    observed_at = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
    payload = build_envelope_drift_alert(
        strategy_key="strategy:btc",
        current_version=3,
        current_value={"max_drawdown_pct": 5.0},
        proposed_value={"max_drawdown_pct": 8.0},
        divergence_pct=0.6,
        originating_characterization_revision="char-rev-42",
        observed_at=observed_at,
    )
    assert payload["category"] == CATEGORY_ENVELOPE_DRIFT_DETECTED
    assert payload["severity"] == SEVERITY_WARNING
    assert payload["strategy_key"] == "strategy:btc"
    assert payload["current_version"] == 3
    assert payload["current_value"] == {"max_drawdown_pct": 5.0}
    assert payload["proposed_value"] == {"max_drawdown_pct": 8.0}
    assert payload["divergence_pct"] == pytest.approx(0.6)
    assert payload["originating_characterization_revision"] == "char-rev-42"
    assert "v3" in payload["message"]
    assert payload["timestamp"].startswith("2026-05-29T12:00:00")
    assert "dedupe_key" in payload


def test_build_envelope_drift_alert_no_prior() -> None:
    """When no operator-approved envelope exists, message wording shifts."""
    payload = build_envelope_drift_alert(
        strategy_key="strategy:eth",
        current_version=None,
        current_value=None,
        proposed_value={"max_drawdown_pct": 4.5},
        divergence_pct=float("inf"),
        originating_characterization_revision="char-rev-99",
    )
    assert payload["current_version"] is None
    assert payload["current_value"] is None
    assert "no operator-approved envelope" in payload["message"]


# ─── AC5.a — divergence math ────────────────────────────────────────────────


def test_compute_divergence_none_current_returns_infinity() -> None:
    assert compute_max_divergence_pct(None, {"x": 1.0}) == float("inf")


def test_compute_divergence_identical_values_returns_zero() -> None:
    assert compute_max_divergence_pct({"x": 1.0}, {"x": 1.0}) == 0.0


def test_compute_divergence_picks_max_across_keys() -> None:
    current = {"a": 5.0, "b": 10.0}
    proposed = {"a": 5.5, "b": 12.0}  # a: 10%, b: 20%
    assert compute_max_divergence_pct(current, proposed) == pytest.approx(0.2)


def test_compute_divergence_handles_zero_baseline() -> None:
    """A current 0.0 → epsilon-denominator avoids div-by-zero (returns large finite)."""
    div = compute_max_divergence_pct({"x": 0.0}, {"x": 0.01})
    assert div > 1_000_000  # epsilon=1e-12 → 1e10-ish


def test_compute_divergence_added_or_removed_key_is_infinity() -> None:
    assert compute_max_divergence_pct({"x": 1.0}, {"x": 1.0, "y": 2.0}) == float("inf")
    assert compute_max_divergence_pct({"x": 1.0, "y": 2.0}, {"x": 1.0}) == float("inf")


def test_compute_divergence_ignores_non_numeric_keys() -> None:
    """Numeric keys are compared; non-numeric keys don't drag the value."""
    current = {"x": 1.0, "label": "old"}
    proposed = {"x": 1.0, "label": "new"}
    assert compute_max_divergence_pct(current, proposed) == 0.0


# ─── AC5.e branch 1 — no_approved ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_branch_no_approved_fires_alert() -> None:
    nats = _RecordingNATS()
    emitter = EnvelopeDriftEmitter(nats_client=nats)
    fired = await emitter.check_and_emit(
        strategy_key="strategy:btc",
        current_version=None,
        current_value=None,
        proposed_value={"max_drawdown_pct": 8.0},
        originating_characterization_revision="char-rev-1",
    )
    assert fired is True
    assert len(nats.published) == 1
    subj, payload = nats.published[0]
    assert subj == "alerts.envelope.drift_detected.strategy:btc"
    assert payload["current_version"] is None
    assert payload["divergence_pct"] == float("inf")


# ─── AC5.e branch 2 — approved_below_threshold ──────────────────────────────


@pytest.mark.asyncio
async def test_branch_approved_below_threshold_is_silent() -> None:
    nats = _RecordingNATS()
    emitter = EnvelopeDriftEmitter(nats_client=nats)
    fired = await emitter.check_and_emit(
        strategy_key="strategy:btc",
        current_version=3,
        current_value={"max_drawdown_pct": 5.0},
        proposed_value={"max_drawdown_pct": 5.4},  # 8% — below 10% default
        originating_characterization_revision="char-rev-1",
    )
    assert fired is False
    assert nats.published == []


# ─── AC5.e branch 3 — approved_above_threshold ──────────────────────────────


@pytest.mark.asyncio
async def test_branch_approved_above_threshold_fires_alert() -> None:
    nats = _RecordingNATS()
    emitter = EnvelopeDriftEmitter(nats_client=nats)
    fired = await emitter.check_and_emit(
        strategy_key="strategy:btc",
        current_version=3,
        current_value={"max_drawdown_pct": 5.0},
        proposed_value={"max_drawdown_pct": 6.0},  # 20% — above 10% default
        originating_characterization_revision="char-rev-1",
    )
    assert fired is True
    assert len(nats.published) == 1
    _, payload = nats.published[0]
    assert payload["divergence_pct"] == pytest.approx(0.2)
    assert payload["current_version"] == 3


# ─── AC5.c — rate-limit suppression ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_suppresses_within_window() -> None:
    nats = _RecordingNATS()
    emitter = EnvelopeDriftEmitter(
        nats_client=nats, rate_limit_window=timedelta(hours=1)
    )
    t0 = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)

    fired_1 = await emitter.check_and_emit(
        strategy_key="strategy:btc",
        current_version=3,
        current_value={"x": 1.0},
        proposed_value={"x": 2.0},
        originating_characterization_revision="char-rev-1",
        observed_at=t0,
    )
    fired_2 = await emitter.check_and_emit(
        strategy_key="strategy:btc",
        current_version=3,
        current_value={"x": 1.0},
        proposed_value={"x": 3.0},
        originating_characterization_revision="char-rev-2",
        observed_at=t0 + timedelta(minutes=30),  # still inside window
    )

    assert fired_1 is True
    assert fired_2 is False
    assert len(nats.published) == 1


@pytest.mark.asyncio
async def test_rate_limit_re_arms_after_window() -> None:
    nats = _RecordingNATS()
    emitter = EnvelopeDriftEmitter(
        nats_client=nats, rate_limit_window=timedelta(hours=1)
    )
    t0 = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)

    await emitter.check_and_emit(
        strategy_key="strategy:btc",
        current_version=3,
        current_value={"x": 1.0},
        proposed_value={"x": 2.0},
        originating_characterization_revision="char-rev-1",
        observed_at=t0,
    )
    fired_2 = await emitter.check_and_emit(
        strategy_key="strategy:btc",
        current_version=3,
        current_value={"x": 1.0},
        proposed_value={"x": 3.0},
        originating_characterization_revision="char-rev-2",
        observed_at=t0 + timedelta(hours=2),  # outside window
    )
    assert fired_2 is True
    assert len(nats.published) == 2


@pytest.mark.asyncio
async def test_rate_limit_is_per_strategy_key() -> None:
    """A different strategy_key is not suppressed by another strategy's recent alert."""
    nats = _RecordingNATS()
    emitter = EnvelopeDriftEmitter(nats_client=nats)
    t0 = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)

    await emitter.check_and_emit(
        strategy_key="strategy:btc",
        current_version=1,
        current_value={"x": 1.0},
        proposed_value={"x": 2.0},
        originating_characterization_revision="char-rev-1",
        observed_at=t0,
    )
    fired_eth = await emitter.check_and_emit(
        strategy_key="strategy:eth",
        current_version=1,
        current_value={"x": 1.0},
        proposed_value={"x": 2.0},
        originating_characterization_revision="char-rev-1",
        observed_at=t0,
    )
    assert fired_eth is True
    assert len(nats.published) == 2


# ─── AC5.d — non-mutation property ──────────────────────────────────────────


def test_emitter_exposes_no_envelope_mutation_methods() -> None:
    """AC5.d — the emitter must not provide a way to update the envelope.

    Asserted by enumerating the emitter's public surface and checking
    nothing looks like a write/update/set/save method.
    """
    public_methods = [m for m in dir(EnvelopeDriftEmitter) if not m.startswith("_")]
    forbidden_prefixes = ("write", "update", "set_", "save", "put", "delete")
    bad = [m for m in public_methods if m.startswith(forbidden_prefixes)]
    assert bad == [], f"emitter exposes mutation-like methods: {bad}"


# ─── AC5.b — default constants are public + sensible ────────────────────────


def test_default_threshold_is_ten_percent() -> None:
    assert DEFAULT_DIVERGENCE_THRESHOLD == pytest.approx(0.10)


def test_default_rate_limit_window_is_one_hour() -> None:
    assert DEFAULT_RATE_LIMIT_WINDOW == timedelta(hours=1)


# ─── NATS-absent best-effort behaviour ──────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_returns_true_even_without_nats_client() -> None:
    """The emitter never crashes when nats_client is None — fires + records."""
    emitter = EnvelopeDriftEmitter(nats_client=None)
    fired = await emitter.check_and_emit(
        strategy_key="strategy:btc",
        current_version=1,
        current_value={"x": 1.0},
        proposed_value={"x": 2.0},
        originating_characterization_revision="char-rev-1",
    )
    assert fired is True
    assert emitter.last_fired_at("strategy:btc") is not None
