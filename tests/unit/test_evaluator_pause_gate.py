"""Tests for the P2.6 evaluator pause gate (#597).

Covers:
  * `EvaluatorSubscriber` parses incoming `evaluator.<subsystem>.verdict`
    messages and surfaces them via `is_paused` / `paused_subsystems`.
  * Operator overrides win over the latest verdict.
  * `SignalArbiter.check()` short-circuits to "suppressed" when any
    pause-guarded subsystem is unhealthy.
  * The /state HTTP routes expose the snapshot and accept overrides.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cio.apps.state_api import router as state_router
from cio.core.arbiter import SignalArbiter
from cio.core.evaluator_subscriber import (
    HEALTHY,
    UNHEALTHY,
    UNKNOWN,
    EvaluatorSubscriber,
)


def _msg(subject: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(subject=subject, data=json.dumps(payload).encode())


@pytest.fixture
def subscriber(mock_nats_client):
    return EvaluatorSubscriber(nats_client=mock_nats_client)


@pytest.mark.asyncio
async def test_subscriber_tracks_per_subsystem_verdict(subscriber):
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": HEALTHY, "reason": "ok"})
    )
    assert subscriber.is_paused("ingest") is False

    await subscriber._handle_message(
        _msg(
            "evaluator.ingest.verdict",
            {"verdict": UNHEALTHY, "reason": "binance.futures silent 90s"},
        )
    )
    assert subscriber.is_paused("ingest") is True
    paused = subscriber.paused_subsystems()
    assert len(paused) == 1
    assert paused[0]["subsystem"] == "ingest"
    assert "silent" in paused[0]["reason"]


@pytest.mark.asyncio
async def test_subscriber_resume_on_healthy_verdict(subscriber):
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": UNHEALTHY, "reason": "stale"})
    )
    assert subscriber.is_paused("ingest") is True
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": HEALTHY, "reason": "recovered"})
    )
    assert subscriber.is_paused("ingest") is False


@pytest.mark.asyncio
async def test_subscriber_ignores_malformed_messages(subscriber):
    # Unparseable JSON
    bad = SimpleNamespace(subject="evaluator.ingest.verdict", data=b"not json")
    await subscriber._handle_message(bad)
    # Subject pattern wrong
    await subscriber._handle_message(
        _msg("market.data.tick", {"verdict": UNHEALTHY, "reason": "x"})
    )
    # Verdict invalid
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": "broken", "reason": "x"})
    )
    assert subscriber.is_paused("ingest") is False
    assert subscriber.snapshot()["verdicts"] == []


def test_override_pauses_even_when_verdict_healthy(subscriber):
    # No verdict observed at all — operator manually pauses.
    subscriber.set_override("ingest", UNHEALTHY)
    assert subscriber.is_paused("ingest") is True
    paused = subscriber.paused_subsystems()
    assert paused[0]["override"] == UNHEALTHY


@pytest.mark.asyncio
async def test_override_unpauses_even_when_verdict_unhealthy(subscriber):
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": UNHEALTHY, "reason": "x"})
    )
    subscriber.set_override("ingest", HEALTHY)
    assert subscriber.is_paused("ingest") is False
    # Cleared override returns to verdict-driven state.
    subscriber.set_override("ingest", None)
    assert subscriber.is_paused("ingest") is True


def test_override_validates_verdict(subscriber):
    with pytest.raises(ValueError) as exc_info:
        subscriber.set_override("ingest", "not-a-verdict")
    assert "healthy" in str(exc_info.value)


@pytest.mark.asyncio
async def test_arbiter_short_circuits_when_ingest_unhealthy(
    subscriber, mock_redis_cache
):
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": UNHEALTHY, "reason": "silent"})
    )
    arbiter = SignalArbiter(cache=mock_redis_cache, evaluator_subscriber=subscriber)
    allowed, reason = await arbiter.check(
        symbol="BTCUSDT",
        action="buy",
        confidence=0.9,
        strategy_id="ta-momentum",
        correlation_id="corr-1",
    )
    assert allowed is False
    assert "ARBITER_PAUSED" in reason
    # The Redis cache must NOT be touched when arbitration is paused —
    # that's the whole point of the early-exit guard.
    mock_redis_cache.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_arbiter_passes_through_when_ingest_healthy(subscriber, mock_redis_cache):
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": HEALTHY, "reason": "ok"})
    )
    mock_redis_cache.get = AsyncMock(return_value=None)
    mock_redis_cache.set = AsyncMock()
    arbiter = SignalArbiter(cache=mock_redis_cache, evaluator_subscriber=subscriber)
    allowed, _ = await arbiter.check(
        symbol="BTCUSDT",
        action="buy",
        confidence=0.9,
        strategy_id="ta-momentum",
        correlation_id="corr-1",
    )
    assert allowed is True
    mock_redis_cache.set.assert_awaited()


@pytest.mark.asyncio
async def test_arbiter_passes_through_when_no_subscriber_wired(mock_redis_cache):
    """Legacy/no-eval-subscriber path must not break — ``None`` is allowed."""
    mock_redis_cache.get = AsyncMock(return_value=None)
    mock_redis_cache.set = AsyncMock()
    arbiter = SignalArbiter(cache=mock_redis_cache, evaluator_subscriber=None)
    allowed, _ = await arbiter.check(
        symbol="BTCUSDT",
        action="buy",
        confidence=0.9,
        strategy_id="ta-momentum",
        correlation_id="corr-2",
    )
    assert allowed is True


def _make_app(subscriber):
    app = FastAPI()
    app.state.evaluator_subscriber = subscriber
    app.include_router(state_router)
    return app


@pytest.mark.asyncio
async def test_state_endpoint_returns_snapshot(subscriber):
    await subscriber._handle_message(
        _msg(
            "evaluator.ingest.verdict",
            {"verdict": UNHEALTHY, "reason": "silent for 90s"},
        )
    )
    client = TestClient(_make_app(subscriber))
    r = client.get("/state")
    assert r.status_code == 200
    body = r.json()
    assert len(body["verdicts"]) == 1
    assert len(body["paused"]) == 1
    assert body["paused"][0]["subsystem"] == "ingest"


@pytest.mark.asyncio
async def test_state_override_endpoint(subscriber):
    client = TestClient(_make_app(subscriber))
    r = client.post("/state/override/ingest", json={"verdict": UNHEALTHY})
    assert r.status_code == 200
    assert r.json() == {"subsystem": "ingest", "override": UNHEALTHY}
    assert subscriber.is_paused("ingest") is True


def test_state_endpoint_503_when_subscriber_missing():
    app = FastAPI()
    app.include_router(state_router)
    client = TestClient(app)
    r = client.get("/state")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_unknown_verdict_does_not_pause(subscriber):
    # The spec is explicit: only `unhealthy` pauses. `unknown` (e.g.
    # ingest evaluator before its first message) must allow arbitration.
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": UNKNOWN, "reason": "no msg yet"})
    )
    assert subscriber.is_paused("ingest") is False


# ---------------------------------------------------------------------------
# AC1 — Extended subsystems (P2.6-EXT #123)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscriber_tracks_execution_subsystem(subscriber):
    await subscriber._handle_message(
        _msg("evaluator.execution.verdict", {"verdict": UNHEALTHY, "reason": "timeout"})
    )
    assert subscriber.is_paused("execution") is True


@pytest.mark.asyncio
async def test_subscriber_tracks_strategy_fidelity_subsystem(subscriber):
    await subscriber._handle_message(
        _msg(
            "evaluator.strategy-fidelity.verdict",
            {"verdict": UNHEALTHY, "reason": "drift"},
        )
    )
    assert subscriber.is_paused("strategy-fidelity") is True


@pytest.mark.asyncio
async def test_subscriber_tracks_audit_subsystem(subscriber):
    await subscriber._handle_message(
        _msg("evaluator.audit.verdict", {"verdict": UNHEALTHY, "reason": "gap"})
    )
    assert subscriber.is_paused("audit") is True


# ---------------------------------------------------------------------------
# AC2 — Per-subsystem pause policy (strict vs lax)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arbiter_strict_policy_suppresses_on_execution_unhealthy(
    subscriber, mock_redis_cache
):
    """execution has strict policy — arbiter must suppress the signal."""
    await subscriber._handle_message(
        _msg("evaluator.execution.verdict", {"verdict": UNHEALTHY, "reason": "timeout"})
    )
    arbiter = SignalArbiter(cache=mock_redis_cache, evaluator_subscriber=subscriber)
    allowed, reason = await arbiter.check(
        symbol="BTCUSDT",
        action="buy",
        confidence=0.9,
        strategy_id="ta-momentum",
        correlation_id="corr-exec",
    )
    assert allowed is False
    assert "ARBITER_PAUSED" in reason
    assert "execution" in reason
    mock_redis_cache.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_arbiter_lax_policy_allows_on_strategy_fidelity_unhealthy(
    subscriber, mock_redis_cache
):
    """strategy-fidelity has lax policy — arbiter must warn but allow the signal."""
    from unittest.mock import AsyncMock

    await subscriber._handle_message(
        _msg(
            "evaluator.strategy-fidelity.verdict",
            {"verdict": UNHEALTHY, "reason": "drift"},
        )
    )
    mock_redis_cache.get = AsyncMock(return_value=None)
    mock_redis_cache.set = AsyncMock()
    arbiter = SignalArbiter(cache=mock_redis_cache, evaluator_subscriber=subscriber)
    allowed, _ = await arbiter.check(
        symbol="BTCUSDT",
        action="buy",
        confidence=0.9,
        strategy_id="ta-momentum",
        correlation_id="corr-fidelity",
    )
    assert allowed is True
    mock_redis_cache.set.assert_awaited()


@pytest.mark.asyncio
async def test_arbiter_lax_policy_allows_on_audit_unhealthy(
    subscriber, mock_redis_cache
):
    """audit has lax policy — arbiter must warn but allow the signal."""
    from unittest.mock import AsyncMock

    await subscriber._handle_message(
        _msg("evaluator.audit.verdict", {"verdict": UNHEALTHY, "reason": "gap"})
    )
    mock_redis_cache.get = AsyncMock(return_value=None)
    mock_redis_cache.set = AsyncMock()
    arbiter = SignalArbiter(cache=mock_redis_cache, evaluator_subscriber=subscriber)
    allowed, _ = await arbiter.check(
        symbol="ETHUSDT",
        action="sell",
        confidence=0.7,
        strategy_id="ta-trend",
        correlation_id="corr-audit",
    )
    assert allowed is True


# ---------------------------------------------------------------------------
# AC3 — Pause/resume audit trail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_trail_records_pause_event(subscriber):
    await subscriber._handle_message(
        _msg("evaluator.execution.verdict", {"verdict": UNHEALTHY, "reason": "timeout"})
    )
    log = subscriber.pause_audit_log()
    assert len(log) == 1
    assert log[0]["subsystem"] == "execution"
    assert log[0]["event"] == "paused"
    assert log[0]["verdict"] == UNHEALTHY
    assert "entry_id" in log[0]
    assert "timestamp" in log[0]


@pytest.mark.asyncio
async def test_audit_trail_records_resume_event(subscriber):
    await subscriber._handle_message(
        _msg("evaluator.execution.verdict", {"verdict": UNHEALTHY, "reason": "timeout"})
    )
    await subscriber._handle_message(
        _msg("evaluator.execution.verdict", {"verdict": HEALTHY, "reason": "recovered"})
    )
    log = subscriber.pause_audit_log()
    events = [e["event"] for e in log]
    assert "paused" in events
    assert "resumed" in events


@pytest.mark.asyncio
async def test_audit_trail_not_duplicated_on_same_verdict(subscriber):
    """Repeating the same verdict must not add a new audit entry."""
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": UNHEALTHY, "reason": "x"})
    )
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": UNHEALTHY, "reason": "still bad"})
    )
    log = subscriber.pause_audit_log()
    assert len(log) == 1  # only the first transition


@pytest.mark.asyncio
async def test_snapshot_includes_pause_audit_log(subscriber):
    await subscriber._handle_message(
        _msg("evaluator.ingest.verdict", {"verdict": UNHEALTHY, "reason": "stale"})
    )
    snap = subscriber.snapshot()
    assert "pause_audit_log" in snap
    assert len(snap["pause_audit_log"]) == 1


# ---------------------------------------------------------------------------
# AC4 — Integration: execution unhealthy → pause → healthy → resume → audit intact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac4_execution_pause_resume_full_cycle(subscriber, mock_redis_cache):
    """AC4: execution evaluator transitions to unhealthy → CIO pauses
    arbitration → evaluator recovers → CIO resumes → audit trail intact."""
    from unittest.mock import AsyncMock

    mock_redis_cache.get = AsyncMock(return_value=None)
    mock_redis_cache.set = AsyncMock()
    arbiter = SignalArbiter(cache=mock_redis_cache, evaluator_subscriber=subscriber)

    # 1. Healthy baseline — signal passes.
    await subscriber._handle_message(
        _msg("evaluator.execution.verdict", {"verdict": HEALTHY, "reason": "ok"})
    )
    allowed, _ = await arbiter.check("BTCUSDT", "buy", 0.9, "s1", "c1")
    assert allowed is True

    # 2. Execution evaluator becomes unhealthy — arbiter pauses (strict policy).
    await subscriber._handle_message(
        _msg(
            "evaluator.execution.verdict",
            {"verdict": UNHEALTHY, "reason": "executor silent 30s"},
        )
    )
    assert subscriber.is_paused("execution") is True
    allowed, reason = await arbiter.check("BTCUSDT", "buy", 0.9, "s1", "c2")
    assert allowed is False
    assert "ARBITER_PAUSED" in reason

    # 3. Execution evaluator recovers — arbiter resumes.
    await subscriber._handle_message(
        _msg(
            "evaluator.execution.verdict",
            {"verdict": HEALTHY, "reason": "executor responsive"},
        )
    )
    assert subscriber.is_paused("execution") is False
    mock_redis_cache.get = AsyncMock(return_value=None)
    allowed, _ = await arbiter.check("BTCUSDT", "buy", 0.9, "s1", "c3")
    assert allowed is True

    # 4. Audit trail has both the pause and the resume event.
    log = subscriber.pause_audit_log()
    events = {e["event"] for e in log if e["subsystem"] == "execution"}
    assert "paused" in events
    assert "resumed" in events
