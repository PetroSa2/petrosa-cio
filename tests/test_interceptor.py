"""Tests for CIO intent interception flow."""

import json
import statistics
import time

import pytest

from apps.nurse.enforcer import EnforcerResult
from core.nats.interceptor import NurseInterceptor


class FakeNatsClient:
    def __init__(self):
        self.subscriptions = []
        self.published = []

    async def subscribe(self, subject, cb):
        self.subscriptions.append((subject, cb))

    async def publish(self, subject, payload, headers=None):
        self.published.append((subject, payload, headers or {}))


class FakeEnforcer:
    def __init__(self, approved=True, reason=None, metadata=None):
        self.approved = approved
        self.reason = reason
        self.metadata = metadata

    async def enforce(self, payload):
        return EnforcerResult(
            approved=self.approved, reason=self.reason, metadata=self.metadata
        )


class FakeRoiLogger:
    def __init__(self):
        self.documents = []

    async def log_audit(self, document):
        self.documents.append(document)


@pytest.mark.asyncio
async def test_start_subscribes_to_intent_subject():
    client = FakeNatsClient()
    interceptor = NurseInterceptor(client)

    await interceptor.start()

    assert len(client.subscriptions) == 1
    assert client.subscriptions[0][0] == "cio.intent.>"


@pytest.mark.asyncio
async def test_approved_intent_promotes_signal_and_logs_trace_context():
    client = FakeNatsClient()
    roi_logger = FakeRoiLogger()
    interceptor = NurseInterceptor(
        nats_client=client,
        enforcer=FakeEnforcer(approved=True),
        roi_logger=roi_logger,
    )

    intent = {
        "strategy_id": "strat-1",
        "symbol": "BTCUSDT",
        "action": "buy",
        "confidence": 0.9,
        "potential_pnl": 42.5,
    }

    result = await interceptor.handle_intent(
        json.dumps(intent).encode(),
        headers={"traceparent": "00-abcd-efgh-01"},
    )

    assert result["approved"] is True
    assert len(client.published) == 1
    subject, payload, headers = client.published[0]
    promoted = json.loads(payload.decode())

    assert subject == "signals.trading"
    assert promoted["strategy_id"] == intent["strategy_id"]
    assert promoted["symbol"] == intent["symbol"]
    assert promoted["_otel_trace_context"]["traceparent"] == "00-abcd-efgh-01"
    assert headers["traceparent"] == "00-abcd-efgh-01"

    assert len(roi_logger.documents) == 1
    audit = roi_logger.documents[0]
    assert audit["status"] == "Approved"
    assert audit["potential_pnl"] == pytest.approx(42.5)
    assert audit["latency_budget_met"] is True


@pytest.mark.asyncio
async def test_blocked_intent_does_not_publish_and_is_audited():
    client = FakeNatsClient()
    roi_logger = FakeRoiLogger()
    interceptor = NurseInterceptor(
        nats_client=client,
        enforcer=FakeEnforcer(approved=False, reason="policy_block"),
        roi_logger=roi_logger,
    )

    intent = {"symbol": "BTCUSDT", "action": "buy", "expected_pnl": "11.2"}
    result = await interceptor.handle_intent(json.dumps(intent).encode(), headers={})

    assert result["approved"] is False
    assert client.published == []
    assert roi_logger.documents[0]["status"] == "Blocked"
    assert roi_logger.documents[0]["reason"] == "policy_block"
    assert roi_logger.documents[0]["potential_pnl"] == pytest.approx(11.2)


@pytest.mark.asyncio
async def test_semantic_veto_is_logged_with_regime_and_saved_capital_metadata():
    client = FakeNatsClient()
    roi_logger = FakeRoiLogger()
    interceptor = NurseInterceptor(
        nats_client=client,
        enforcer=FakeEnforcer(
            approved=False,
            reason="drawdown_limit_exceeded",
            metadata={
                "veto_type": "semantic",
                "current_regime": "bearish",
                "drawdown_limit_exceeded": True,
                "vol_threshold_breach": False,
                "saved_capital": 12.4,
            },
        ),
        roi_logger=roi_logger,
    )

    result = await interceptor.handle_intent(
        json.dumps(
            {"symbol": "BTCUSDT", "action": "buy", "expected_pnl": 2.5}
        ).encode(),
        headers={},
    )

    assert result["approved"] is False
    assert client.published == []
    audit = roi_logger.documents[0]
    assert audit["veto_type"] == "semantic"
    assert audit["regime_metadata"]["current_regime"] == "bearish"
    assert audit["regime_metadata"]["drawdown_limit_exceeded"] is True
    assert audit["pnl_metadata"]["potential_pnl"] == pytest.approx(2.5)
    assert audit["pnl_metadata"]["saved_capital"] == pytest.approx(12.4)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_interceptor_latency_p95_under_50ms_for_100_messages():
    client = FakeNatsClient()
    roi_logger = FakeRoiLogger()
    interceptor = NurseInterceptor(
        nats_client=client,
        enforcer=FakeEnforcer(approved=True),
        roi_logger=roi_logger,
        max_latency_ms=50.0,
    )

    latencies = []
    for idx in range(100):
        payload = {
            "strategy_id": f"strat-{idx}",
            "symbol": "BTCUSDT",
            "action": "buy",
            "confidence": 0.8,
            "potential_pnl": idx / 10.0,
        }
        start = time.perf_counter()
        result = await interceptor.handle_intent(
            json.dumps(payload).encode(), headers={}
        )
        _ = result
        latencies.append((time.perf_counter() - start) * 1000.0)

    # P95 over 100 samples corresponds to index 94 in sorted list.
    p95 = sorted(latencies)[94]

    assert p95 < 50.0
    assert statistics.mean(latencies) < 50.0
    assert len(client.published) == 100
    assert len(roi_logger.documents) == 100
