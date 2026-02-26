"""Tests for NATS heartbeat service."""

import json
import statistics
import time
from unittest.mock import AsyncMock

import pytest

from core.nats.heartbeat import HeartbeatService


class FakeMsg:
    def __init__(self):
        self.replies = []

    async def respond(self, payload: bytes) -> None:
        self.replies.append(payload)


@pytest.mark.asyncio
async def test_build_heartbeat_includes_liveness_metadata():
    service = HeartbeatService(version="1.0.0")
    service.check_redis_health = AsyncMock(return_value=True)
    service.check_mongo_health = AsyncMock(return_value=True)

    payload = await service.build_heartbeat()

    assert payload["status"] == "OK"
    assert payload["status_code"] == "GOVERNANCE_ACTIVE"
    assert payload["version"] == "1.0.0"
    assert "timestamp" in payload
    assert payload["dependencies"]["redis"] == "connected"
    assert payload["dependencies"]["mongo"] == "connected"


@pytest.mark.asyncio
async def test_build_heartbeat_marks_degraded_on_dependency_failure():
    service = HeartbeatService(version="1.0.0")
    service.check_redis_health = AsyncMock(return_value=True)
    service.check_mongo_health = AsyncMock(return_value=False)

    payload = await service.build_heartbeat()

    assert payload["status"] == "OK"
    assert payload["status_code"] == "DEGRADED"
    assert payload["dependencies"]["redis"] == "connected"
    assert payload["dependencies"]["mongo"] == "disconnected"


@pytest.mark.asyncio
async def test_handle_request_replies_with_json_payload():
    service = HeartbeatService(version="1.0.0")
    service.check_redis_health = AsyncMock(return_value=True)
    service.check_mongo_health = AsyncMock(return_value=True)

    msg = FakeMsg()
    await service.handle_request(msg)

    assert len(msg.replies) == 1
    payload = json.loads(msg.replies[0].decode())
    assert payload["status"] == "OK"
    assert payload["version"] == "1.0.0"


@pytest.mark.asyncio
@pytest.mark.performance
async def test_heartbeat_p95_under_20ms_for_100_requests():
    service = HeartbeatService(version="1.0.0")
    service.check_redis_health = AsyncMock(return_value=True)
    service.check_mongo_health = AsyncMock(return_value=True)

    latencies = []
    for _ in range(100):
        start = time.perf_counter()
        await service.build_heartbeat()
        latencies.append((time.perf_counter() - start) * 1000.0)

    p95 = sorted(latencies)[94]
    assert p95 < 20.0
    assert statistics.mean(latencies) < 20.0
