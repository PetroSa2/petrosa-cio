"""Tests for the CIO health evaluator (P7.1, #610).

Covers the three failure-injection cases from the ticket's acceptance
criteria:

  (a) CIO decision with missing reasoning context  → unhealthy
  (b) sustained FAIL_SAFE dominance window         → unhealthy (degraded)
  (c) silence on signals.trading.> while intents flowing → unhealthy

Plus structural coverage of the publish path, hysteresis, idle/unknown
state, and the OutcomeCorrelator's persistence hook.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from cio.core.health_evaluator import (
    DECISION_AUDIT_PATTERN,
    HEALTHY,
    INTENT_PATTERN,
    SIGNAL_PATTERN,
    UNHEALTHY,
    UNKNOWN,
    VERDICT_SUBJECT,
    CIOHealthEvaluator,
    OutcomeCorrelator,
)


def _msg(subject: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(subject=subject, data=json.dumps(payload).encode())


@pytest.fixture
def evaluator(mock_nats_client):
    return CIOHealthEvaluator(
        nats_client=mock_nats_client,
        window=timedelta(seconds=60),
        emit_interval=timedelta(seconds=15),
        stable_ticks_required=1,  # default 1 in tests → no flap delay
        missing_context_threshold=0.5,
        degraded_threshold=0.5,
        min_intents_for_silence_check=3,
        silence_min_age=timedelta(seconds=20),
    )


async def _decision(
    evaluator: CIOHealthEvaluator,
    *,
    action: str,
    thought_trace: str = "regime_choppy → SKIP",
    decision_id: str = "d-1",
    strategy_id: str = "ta-momentum",
    correlation_id: str = "c-1",
) -> None:
    payload = {
        "decision_id": decision_id,
        "correlation_id": correlation_id,
        "strategy_id": strategy_id,
        "action": action,
        "thought_trace": thought_trace,
        "justification": "test",
    }
    await evaluator._on_decision(_msg(f"cio.decision.audit.{action}", payload))


# ---------------------------------------------------------------------------
# AC (a): missing reasoning context → unhealthy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_reasoning_context_yields_unhealthy(evaluator):
    # Dominantly empty / fallback-marker thought traces. The fallback
    # marker set comes straight from cio.models.decision SAFE_DEFAULTS.
    for i, trace in enumerate(["PARSE_FAILURE", "", "SYSTEM_ERROR", "PARSE_FAILURE"]):
        await _decision(
            evaluator,
            action="execute",
            thought_trace=trace,
            decision_id=f"d-{i}",
        )
    # A single well-reasoned decision is not enough to recover when the
    # fallback fraction exceeds the threshold.
    await _decision(evaluator, action="execute", thought_trace="solid trace")

    verdict, reason = evaluator.evaluate()
    assert verdict == UNHEALTHY
    assert "reasoning-context" in reason or "fallback" in reason


# ---------------------------------------------------------------------------
# AC (b): sustained FAIL_SAFE / SKIP dominance → unhealthy (degraded mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_safe_dominance_yields_degraded_unhealthy(evaluator):
    # Use non-fallback thought traces so the degraded-mode check (the
    # behavioral signal) wins over the structural reasoning-context
    # check — when both fire, the more specific structural symptom
    # takes precedence by design, so this test isolates the behavioral
    # path with traces that look reasoned.
    for i in range(4):
        await _decision(
            evaluator,
            action="fail_safe",
            thought_trace=f"upstream health failing: ingest stale for {i}s",
            decision_id=f"fs-{i}",
        )
    await _decision(
        evaluator,
        action="execute",
        thought_trace="good",
        decision_id="exec-1",
    )

    verdict, reason = evaluator.evaluate()
    assert verdict == UNHEALTHY
    # The reason must name the degraded mode so the operator can act.
    assert "degraded" in reason.lower() or "FAIL_SAFE" in reason


@pytest.mark.asyncio
async def test_skip_dominance_also_counts_as_degraded(evaluator):
    # SKIP dominance is the same family of failure — the SAFE_DEFAULTS
    # action classifier emits SKIP on parse failure, so a window
    # dominated by SKIP is a degraded-mode signal too.
    for i in range(3):
        await _decision(
            evaluator,
            action="skip",
            thought_trace="PARSE_FAILURE",
            decision_id=f"sk-{i}",
        )
    await _decision(
        evaluator,
        action="execute",
        thought_trace="ok",
        decision_id="exec-1",
    )
    # 75% SKIP — but wait, all three SKIP entries also have
    # PARSE_FAILURE traces, so the missing-context check fires first.
    # That's the correct precedence: reasoning context is the more
    # specific symptom. Re-run with non-fallback traces on the SKIPs
    # to isolate the degraded-mode path.
    evaluator._decisions.clear()
    for i in range(3):
        await _decision(
            evaluator,
            action="skip",
            thought_trace="cooldown active for strategy",
            decision_id=f"sk-{i}",
        )
    await _decision(
        evaluator,
        action="execute",
        thought_trace="ok",
        decision_id="exec-1",
    )

    verdict, reason = evaluator.evaluate()
    assert verdict == UNHEALTHY
    assert "degraded" in reason.lower() or "FAIL_SAFE" in reason


# ---------------------------------------------------------------------------
# AC (c): intents flowing but signals silent → unhealthy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intents_flowing_no_signals_yields_unhealthy(evaluator):
    # 4 intents back-dated to look like a sustained flow over the past
    # ~30s. No signals, no decisions — CIO is receiving but not
    # producing.
    now = datetime.now(UTC)
    intent_age = timedelta(seconds=30)
    for i in range(4):
        await evaluator._on_intent(
            _msg(f"cio.intent.trading.BTCUSDT.{i}", {"symbol": "BTCUSDT"})
        )
    # Backdate the intents so the silence_min_age check fires.
    for tick in evaluator._intents:
        tick.observed_at = now - intent_age

    verdict, reason = evaluator.evaluate(now=now)
    assert verdict == UNHEALTHY
    assert "silence" in reason.lower() or "no signals" in reason.lower()


@pytest.mark.asyncio
async def test_fresh_intent_burst_does_not_trigger_silence_check(evaluator):
    # The silence check must NOT fire when intents are fresh enough that
    # CIO simply hasn't had time to respond yet (silence_min_age = 20s).
    for i in range(4):
        await evaluator._on_intent(
            _msg(f"cio.intent.trading.BTCUSDT.{i}", {"symbol": "BTCUSDT"})
        )

    verdict, reason = evaluator.evaluate()
    assert verdict == UNKNOWN
    assert "no recent CIO decisions" in reason


@pytest.mark.asyncio
async def test_signals_present_keeps_evaluator_quiet_on_silence(evaluator):
    # Even if intents are flowing AND no decisions are in the window,
    # the presence of signal output on signals.trading.> means CIO is
    # producing — the silence rule must not fire.
    now = datetime.now(UTC)
    for i in range(4):
        await evaluator._on_intent(_msg(f"cio.intent.trading.{i}", {}))
        await evaluator._on_signal(_msg(f"signals.trading.s-{i}", {}))
    for tick in evaluator._intents:
        tick.observed_at = now - timedelta(seconds=30)
    for tick in evaluator._signals:
        tick.observed_at = now - timedelta(seconds=25)

    verdict, _ = evaluator.evaluate(now=now)
    assert verdict == UNKNOWN  # no decisions in window, but no silence either


# ---------------------------------------------------------------------------
# Steady-state behaviors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_when_decisions_well_reasoned(evaluator):
    for i in range(5):
        await _decision(
            evaluator,
            action="execute",
            thought_trace=f"regime trend; momentum positive; {i}",
            decision_id=f"e-{i}",
        )
    verdict, reason = evaluator.evaluate()
    assert verdict == HEALTHY
    assert "decisions in last" in reason


@pytest.mark.asyncio
async def test_empty_window_yields_unknown(evaluator):
    verdict, reason = evaluator.evaluate()
    assert verdict == UNKNOWN
    assert "no recent" in reason.lower()


@pytest.mark.asyncio
async def test_window_prune_removes_stale_records(evaluator):
    # Inject a decision and immediately backdate it past the window.
    await _decision(evaluator, action="execute", thought_trace="solid")
    for rec in evaluator._decisions:
        rec.observed_at = datetime.now(UTC) - timedelta(seconds=120)

    verdict, _ = evaluator.evaluate()
    assert verdict == UNKNOWN  # stale record dropped


# ---------------------------------------------------------------------------
# Publish path + hysteresis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_publishes_on_verdict_subject(evaluator, mock_nats_client):
    for i in range(3):
        await _decision(
            evaluator,
            action="execute",
            thought_trace="solid",
            decision_id=f"e-{i}",
        )
    verdict, reason = await evaluator.tick()
    assert verdict == HEALTHY
    mock_nats_client.publish.assert_awaited_once()
    args, _ = mock_nats_client.publish.call_args
    subject, payload = args
    assert subject == VERDICT_SUBJECT
    body = json.loads(payload.decode())
    assert body["verdict"] == HEALTHY
    assert body["reason"]  # non-empty
    assert len(body["reason"]) <= 200


@pytest.mark.asyncio
async def test_hysteresis_delays_verdict_flip(mock_nats_client):
    ev = CIOHealthEvaluator(
        nats_client=mock_nats_client,
        window=timedelta(seconds=60),
        emit_interval=timedelta(seconds=15),
        stable_ticks_required=2,
    )
    # The initial emitted state is UNKNOWN; flipping to HEALTHY also
    # requires the hysteresis to clear. Seed healthy decisions and tick
    # twice to settle into HEALTHY before testing the unhealthy flip.
    for i in range(3):
        await _decision(
            ev,
            action="execute",
            thought_trace=f"solid {i}",
            decision_id=f"h-{i}",
        )
    settle1, _ = await ev.tick()
    settle2, _ = await ev.tick()
    assert settle1 == UNKNOWN  # first observation pending
    assert settle2 == HEALTHY  # second observation flips

    # Now flood with FAIL_SAFE. First tick must NOT flip (hysteresis).
    ev._decisions.clear()
    for i in range(4):
        await _decision(
            ev,
            action="fail_safe",
            thought_trace="upstream stale, safe-failing",
            decision_id=f"fs-{i}",
        )
    flip1, _ = await ev.tick()
    flip2, _ = await ev.tick()
    assert flip1 == HEALTHY  # hysteresis holding
    assert flip2 == UNHEALTHY  # flipped after second consecutive raw observation


@pytest.mark.asyncio
async def test_emit_truncates_long_reason_to_200_chars(evaluator, mock_nats_client):
    # Construct a synthetic-but-realistic many-decision corpus that will
    # cause the reason string to approach 200 chars; the publish path
    # must hard-cap at 200 regardless.
    for i in range(40):
        await _decision(
            evaluator,
            action="fail_safe",
            thought_trace="CRITICAL_FAILURE_ENFORCEMENT" + "x" * 20,
            decision_id=f"fs-{i}",
        )
    await evaluator.tick()
    args, _ = mock_nats_client.publish.call_args
    _, payload = args
    body = json.loads(payload.decode())
    assert len(body["reason"]) <= 200


# ---------------------------------------------------------------------------
# Lifecycle (subscribe / unsubscribe)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_subscribes_to_three_subjects(mock_nats_client):
    ev = CIOHealthEvaluator(nats_client=mock_nats_client)
    await ev.start()
    subjects = [call.args[0] for call in mock_nats_client.subscribe.call_args_list]
    assert DECISION_AUDIT_PATTERN in subjects
    assert INTENT_PATTERN in subjects
    assert SIGNAL_PATTERN in subjects
    await ev.stop()


# ---------------------------------------------------------------------------
# Outcome correlator (Phase-2 substrate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcome_correlator_persists_record():
    vc = AsyncMock()
    correlator = OutcomeCorrelator(vector_client=vc)
    await correlator.record(
        decision_id="d-1",
        strategy_id="ta-momentum",
        correlation_id="c-1",
        outcome_payload={"realized_pnl_usd": 12.5},
    )
    vc.upsert.assert_awaited_once()
    kwargs = vc.upsert.call_args.kwargs
    assert kwargs["strategy_id"] == "ta-momentum"
    payload = kwargs["payload"]
    assert payload["event_type"] == "decision_outcome_correlation"
    assert payload["decision_id"] == "d-1"
    assert payload["outcome"]["realized_pnl_usd"] == 12.5


@pytest.mark.asyncio
async def test_outcome_correlator_swallows_vector_errors():
    vc = AsyncMock()
    vc.upsert.side_effect = RuntimeError("vector unreachable")
    correlator = OutcomeCorrelator(vector_client=vc)
    # MUST NOT raise — correlation persistence is best-effort.
    await correlator.record(
        decision_id="d-1",
        strategy_id="ta",
        correlation_id="c-1",
        outcome_payload={},
    )


# ---------------------------------------------------------------------------
# Malformed input resilience (consistent with EvaluatorSubscriber posture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unparsable_decision_payload_dropped(evaluator):
    bad = SimpleNamespace(subject="cio.decision.audit.execute", data=b"not json")
    await evaluator._on_decision(bad)
    assert not evaluator._decisions
