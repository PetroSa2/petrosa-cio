from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.core.auto_resume import (
    HEALTH_PROBE_PROMPT_ID,
    HEALTH_PROBE_SYSTEM_PROMPT,
    AutoResumeLoop,
    LLMHealthTracker,
    LLMPauseRegistry,
    PauseEntry,
    build_resume_request,
    instrument_llm_health,
)
from cio.models.enums import RejectionSource
from cio.models.llm import RawLLMResponse


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeCache:
    def __init__(self):
        self.hashes: dict[str, str] = {}
        self.values: dict[str, str] = {}

    async def hget(self, key: str, field: str):
        return self.hashes.get(field)

    async def hset(self, key: str, field: str, value: str):
        self.hashes[field] = value

    async def hdel(self, key: str, field: str):
        self.hashes.pop(field, None)

    async def hgetall(self, key: str):
        return dict(self.hashes)

    async def get(self, key: str):
        return self.values.get(key)

    async def set_if_absent(self, key: str, value: str, ttl: int):
        if key in self.values:
            return False
        self.values[key] = value
        return True


def response(status_code: int, body: dict):
    result = MagicMock(status_code=status_code, text="")
    result.json.return_value = body
    return result


def raw_response(content: str = "{}", error: str | None = None):
    return RawLLMResponse(
        prompt_id="test",
        content=content,
        error=error,
        model="test",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
    )


def healthy_tracker(clock: FakeClock) -> LLMHealthTracker:
    tracker = LLMHealthTracker(clock=clock)
    for _ in range(10):
        tracker.record(True)
    return tracker


def test_health_tracker_and_reason_code():
    clock = FakeClock()
    tracker = LLMHealthTracker(clock=clock)
    for _ in range(9):
        tracker.record(True)
    assert tracker.is_healthy() is False
    tracker.record(True)
    assert tracker.is_healthy() is True
    assert RejectionSource.LLM_UNAVAILABLE.value == "llm_unavailable"


def test_build_resume_request_shape():
    method, url, payload = build_resume_request("http://ta-bot", "doji", 0)
    assert method == "POST"
    assert url.endswith("/api/v1/strategies/doji/config")
    assert payload["parameters"] == {"enabled": True}
    assert payload["changed_by"] == "petrosa-cio:resume:doji"
    assert payload["validate_only"] is False
    assert payload["reason"].startswith("CIO_AUTO_RESUME: ")


@pytest.mark.asyncio
async def test_registry_records_pause_and_counts_flap():
    clock = FakeClock()
    registry = LLMPauseRegistry(FakeCache(), clock=clock)
    await registry.record_pause("doji", "ta-bot")
    entry = await registry.get("doji")
    assert entry is not None
    assert entry.min_pause_seconds == 1800
    clock.now += 1000
    entry.status = "resumed"
    entry.resumed_at = clock.now - 100
    await registry.put(entry)
    await registry.record_pause("doji", "ta-bot")
    entry = await registry.get("doji")
    assert entry is not None
    assert entry.flap_count == 1
    assert entry.min_pause_seconds == 3600


@pytest.mark.asyncio
async def test_instrument_llm_health_is_idempotent():
    clock = FakeClock()
    tracker = LLMHealthTracker(clock=clock)

    class Client:
        async def complete(self, prompt_id, system_prompt, user_context):
            return raw_response()

    client = Client()
    instrument_llm_health(client, tracker)
    instrument_llm_health(client, tracker)

    await client.complete("p", "s", {})
    assert tracker.sample_count() == 1


@pytest.mark.asyncio
async def test_resume_happy_path_and_foreign_change_guard():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    await registry.record_pause("doji", "ta-bot")
    clock.now += 1801
    tracker = healthy_tracker(clock)
    http = MagicMock()
    http.get = AsyncMock(
        return_value=response(
            200,
            {
                "success": True,
                "data": [
                    {
                        "changed_by": "petrosa-cio:doji",
                        "changed_at": datetime.fromtimestamp(1000, UTC).isoformat(),
                    }
                ],
            },
        )
    )
    http.request = AsyncMock(return_value=response(200, {"success": True}))
    loop = AutoResumeLoop(
        registry,
        tracker,
        MagicMock(),
        http,
        {"ta-bot": "http://ta-bot"},
        cache,
        AsyncMock(),
        clock=clock,
    )

    await loop.tick()
    entry = await registry.get("doji")
    assert entry is not None and entry.status == "resumed"
    assert http.request.await_count == 1

    entry.status = "paused"
    entry.paused_at = clock.now
    entry.last_unavailable_at = clock.now
    entry.resumed_at = None
    await registry.put(entry)
    clock.now += 1801
    cache.values.pop("cio:auto_resume:lock:doji", None)
    for _ in range(10):
        tracker.record(True)
    http.get.return_value = response(
        200,
        {
            "success": True,
            "data": [
                {
                    "changed_by": "operator",
                    "changed_at": datetime.fromtimestamp(clock.now, UTC).isoformat(),
                }
            ],
        },
    )
    await loop.tick()
    assert await registry.get("doji") is None


@pytest.mark.asyncio
async def test_probe_runs_only_for_insufficient_health_samples():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    entry = PauseEntry("doji", "ta-bot", "paused", 0, 0, 0)
    await registry.put(entry)
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=raw_response())
    loop = AutoResumeLoop(
        registry,
        LLMHealthTracker(clock=clock),
        llm,
        MagicMock(),
        {"ta-bot": "http://ta-bot"},
        cache,
        AsyncMock(),
        clock=clock,
    )

    await loop.tick()
    llm.complete.assert_awaited_once_with(
        HEALTH_PROBE_PROMPT_ID,
        HEALTH_PROBE_SYSTEM_PROMPT,
        {"probe": "cio_auto_resume"},
    )
