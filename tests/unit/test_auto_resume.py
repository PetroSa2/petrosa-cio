import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.core.auto_resume import (
    HEALTH_PROBE_PROMPT_ID,
    HEALTH_PROBE_SYSTEM_PROMPT,
    MAX_ATTEMPTS,
    MAX_FLAPS,
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


def test_health_tracker_ratio_tail_and_expiry():
    clock = FakeClock()
    tracker = LLMHealthTracker(clock=clock)
    for ok in [True] * 8 + [False] * 2:
        tracker.record(ok)
    assert tracker.success_ratio() == 0.8
    assert tracker.is_healthy() is False
    tracker.record(True)
    tracker.record(True)
    tracker.record(True)
    assert tracker.is_healthy() is False
    clock.now += 601
    assert tracker.sample_count() == 0
    assert tracker.success_ratio() == 0.0


def test_auto_resume_enabled_reads_flag(monkeypatch):
    from cio.core.auto_resume import auto_resume_enabled

    monkeypatch.setenv("CIO_AUTO_RESUME_ENABLED", "off")
    assert auto_resume_enabled() is False
    monkeypatch.setenv("CIO_AUTO_RESUME_ENABLED", "YES")
    assert auto_resume_enabled() is True


@pytest.mark.asyncio
async def test_instrument_llm_health_records_errors_and_reraises():
    clock = FakeClock()
    tracker = LLMHealthTracker(clock=clock)

    class Client:
        async def complete(self, prompt_id, system_prompt, user_context):
            raise RuntimeError("transport")

    client = Client()
    instrument_llm_health(client, tracker)
    with pytest.raises(RuntimeError):
        await client.complete("p", "s", {})
    assert tracker.sample_count() == 1


def test_pause_entry_rejects_invalid_json():
    assert PauseEntry.from_json(123) is None
    assert PauseEntry.from_json("[]") is None
    assert PauseEntry.from_json("not-json") is None


@pytest.mark.asyncio
async def test_registry_touches_removes_and_swallows_cache_errors():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    await registry.record_pause("doji", "ta-bot")
    clock.now += 100
    await registry.touch_unavailable("doji")
    entry = await registry.get("doji")
    assert entry is not None and entry.last_unavailable_at == clock.now
    await registry.remove("doji", "test")
    assert await registry.get("doji") is None

    class BrokenCache:
        async def hget(self, *args):
            raise RuntimeError("down")

        async def hset(self, *args):
            raise RuntimeError("down")

        async def hdel(self, *args):
            raise RuntimeError("down")

        async def hgetall(self, *args):
            raise RuntimeError("down")

    broken = LLMPauseRegistry(BrokenCache(), clock=clock)
    await broken.record_pause("doji", "ta-bot")
    await broken.touch_unavailable("doji")
    await broken.remove("doji", "test")
    assert await broken.all() == []


@pytest.mark.asyncio
async def test_registry_skips_bad_entries_and_handles_internal_errors():
    clock = FakeClock()
    cache = FakeCache()
    cache.hashes["bad"] = "{}"
    registry = LLMPauseRegistry(cache, clock=clock)
    assert await registry.all() == []
    registry.get = AsyncMock(side_effect=RuntimeError("boom"))
    await registry.touch_unavailable("doji")
    await registry.record_pause("doji", "ta-bot")
    await registry.remove("doji", "test")

    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    entry = PauseEntry("doji", "ta-bot", "gave_up", 0, 0, 1800)
    await registry.put(entry)
    await registry.record_pause("doji", "ta-bot")
    assert (await registry.get("doji")).status == "gave_up"

    class ListCache(FakeCache):
        async def hgetall(self, key):
            return []

    assert await LLMPauseRegistry(ListCache(), clock=clock).all() == []

    await registry.record_pause("doji", "ta-bot")
    await registry.record_pause("doji", "ta-bot")
    assert (await registry.get("doji")).last_unavailable_at == clock.now


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


def build_loop(clock, cache, registry, http=None, tracker=None, nats=None):
    return AutoResumeLoop(
        registry,
        tracker or healthy_tracker(clock),
        MagicMock(),
        http or MagicMock(),
        {"ta-bot": "http://ta-bot"},
        cache,
        nats or AsyncMock(),
        interval_seconds=0.01,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_loop_survives_tick_errors_and_stops():
    clock = FakeClock()
    loop = build_loop(clock, FakeCache(), LLMPauseRegistry(FakeCache(), clock=clock))
    loop.tick = AsyncMock(side_effect=[RuntimeError("tick"), None])
    await loop.start()
    await asyncio.sleep(0.05)
    await loop.stop()
    assert loop.tick.await_count >= 2


@pytest.mark.asyncio
async def test_tick_cleans_old_entries_and_gives_up_flapping():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    stable = PauseEntry("stable", "ta-bot", "resumed", 0, 0, 1800, resumed_at=-4000)
    expired = PauseEntry("expired", "ta-bot", "gave_up", 0, 0, 1800, gave_up_at=-90000)
    flapping = PauseEntry(
        "flapping", "ta-bot", "paused", 0, 0, 1800, flap_count=MAX_FLAPS
    )
    await registry.put(stable)
    await registry.put(expired)
    await registry.put(flapping)
    nats = AsyncMock()
    loop = build_loop(clock, cache, registry, nats=nats)
    await loop.tick()
    assert await registry.get("stable") is None
    assert await registry.get("expired") is None
    assert (await registry.get("flapping")).status == "gave_up"
    nats.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_precheck_and_request_failures_backoff_and_alert():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    entry = PauseEntry("doji", "ta-bot", "paused", 0, 0, 0, attempts=MAX_ATTEMPTS - 1)
    await registry.put(entry)
    audit = response(
        200,
        {
            "success": True,
            "data": [
                {
                    "changed_by": "petrosa-cio:doji",
                    "changed_at": datetime.fromtimestamp(0, UTC).isoformat(),
                }
            ],
        },
    )
    http = MagicMock(get=AsyncMock(return_value=audit))
    http.request = AsyncMock(return_value=response(500, {"success": False}))
    nats = AsyncMock()
    loop = build_loop(clock, cache, registry, http=http, nats=nats)
    await loop.tick()
    saved = await registry.get("doji")
    assert saved is not None and saved.status == "gave_up"
    assert saved.gave_up_reason == "failed_attempts"
    nats.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_429_defers_without_attempt_and_body_failure_counts():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    entry = PauseEntry("doji", "ta-bot", "paused", 0, 0, 0)
    await registry.put(entry)
    audit = response(
        200,
        {
            "success": True,
            "data": [
                {
                    "changed_by": "petrosa-cio:doji",
                    "changed_at": datetime.fromtimestamp(0, UTC).isoformat(),
                }
            ],
        },
    )
    http = MagicMock(get=AsyncMock(return_value=audit))
    http.request = AsyncMock(return_value=response(429, {"retry_after": 5}))
    loop = build_loop(clock, cache, registry, http=http)
    await loop.tick()
    saved = await registry.get("doji")
    assert saved is not None and saved.attempts == 0 and saved.next_attempt_at == 1060

    cache.values.pop("cio:auto_resume:lock:doji", None)
    saved.next_attempt_at = 0
    await registry.put(saved)
    http.request.return_value = response(
        200, {"success": False, "error": {"code": "bad"}}
    )
    await loop.tick()
    saved = await registry.get("doji")
    assert saved is not None and saved.attempts == 1


@pytest.mark.asyncio
async def test_resume_gates_freeze_lock_and_unroutable():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    entry = PauseEntry("doji", "missing", "paused", 0, 0, 0)
    await registry.put(entry)
    loop = AutoResumeLoop(
        registry,
        healthy_tracker(clock),
        MagicMock(),
        MagicMock(),
        {},
        cache,
        AsyncMock(),
        clock=clock,
    )
    await loop.tick()
    assert (await registry.get("doji")).status == "gave_up"

    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    entry = PauseEntry("doji", "ta-bot", "paused", 0, 0, 0)
    await registry.put(entry)
    cache.values["cio:freeze:doji"] = "LOCKED"
    http = MagicMock(request=AsyncMock())
    loop = build_loop(clock, cache, registry, http=http)
    await loop.tick()
    http.request.assert_not_awaited()

    cache.values.pop("cio:freeze:doji", None)
    cache.values["cio:auto_resume:lock:doji"] = "1"
    await loop.tick()
    http.request.assert_not_awaited()

    cache.values.pop("cio:auto_resume:lock:doji", None)
    registry.get = AsyncMock(return_value=None)
    await loop.tick()
    http.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_precheck_rejects_malformed_audit_data():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    await registry.put(PauseEntry("doji", "ta-bot", "paused", 0, 0, 0))
    http = MagicMock(
        get=AsyncMock(return_value=response(200, {"success": True, "data": [{}]}))
    )
    loop = build_loop(clock, cache, registry, http=http)
    await loop.tick()
    assert (await registry.get("doji")).attempts == 1

    cache.values.pop("cio:auto_resume:lock:doji", None)
    entry = await registry.get("doji")
    entry.attempts = 0
    entry.next_attempt_at = 0
    await registry.put(entry)
    http.get.return_value = response(500, {})
    await loop.tick()
    assert (await registry.get("doji")).attempts == 1


@pytest.mark.asyncio
async def test_loop_start_is_idempotent_and_empty_tick_is_noop():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    loop = build_loop(clock, cache, registry)
    await loop.tick()
    await loop.start()
    task = loop._task
    await loop.start()
    assert loop._task is task
    await loop.stop()


@pytest.mark.asyncio
async def test_tick_attempt_limit_probe_failure_lock_and_max_resumes():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    limited = PauseEntry("limited", "ta-bot", "paused", 0, 0, 0, attempts=MAX_ATTEMPTS)
    await registry.put(limited)
    loop = build_loop(clock, cache, registry)
    await loop.tick()
    assert (await registry.get("limited")).status == "gave_up"

    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    await registry.put(PauseEntry("probe", "ta-bot", "paused", 0, 0, 0))
    llm = MagicMock(complete=AsyncMock(side_effect=RuntimeError("down")))
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
    assert llm.complete.await_count == 1

    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    for index in range(4):
        await registry.put(PauseEntry(f"s{index}", "ta-bot", "paused", 0, 0, 0))
    http = MagicMock()
    http.get = AsyncMock(
        return_value=response(
            200,
            {
                "success": True,
                "data": [
                    {
                        "changed_by": "petrosa-cio:owner",
                        "changed_at": datetime.fromtimestamp(0, UTC).isoformat(),
                    }
                ],
            },
        )
    )
    http.request = AsyncMock(return_value=response(200, {"success": True}))
    loop = build_loop(clock, cache, registry, http=http)
    await loop.tick()
    assert http.request.await_count == 3


@pytest.mark.asyncio
async def test_resume_audit_edges_and_foreign_change():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    http = MagicMock()
    loop = build_loop(clock, cache, registry, http=http)

    cases = [
        {"success": True, "data": []},
        {"success": True, "data": [1]},
        {"success": True, "data": [{}]},
        {"success": True, "data": [{"changed_at": "bad", "changed_by": "x"}]},
        {
            "success": True,
            "data": [{"changed_at": "1970-01-01T00:00:00", "changed_by": 1}],
        },
    ]
    for index, body in enumerate(cases):
        entry = PauseEntry(f"bad{index}", "ta-bot", "paused", 0, 0, 0)
        await registry.put(entry)
        http.get = AsyncMock(return_value=response(200, body))
        await loop._resume_one(entry)
        assert (await registry.get(entry.strategy_id)).attempts == 1

    entry = PauseEntry("foreign", "ta-bot", "paused", 0, 0, 0)
    await registry.put(entry)
    http.get = AsyncMock(
        return_value=response(
            200,
            {
                "success": True,
                "data": [
                    {
                        "changed_by": "operator",
                        "changed_at": "1970-01-01T00:01:00+00:00",
                    }
                ],
            },
        )
    )
    await loop._resume_one(entry)
    assert await registry.get("foreign") is None

    entry = PauseEntry("window", "ta-bot", "paused", 0, 0, 0)
    await registry.put(entry)
    http.get = AsyncMock(
        return_value=response(
            200,
            {
                "success": True,
                "data": [
                    {
                        "changed_by": "petrosa-cio:owner",
                        "changed_at": datetime.fromtimestamp(1, UTC).isoformat(),
                    }
                ]
                * 20,
            },
        )
    )
    await loop._resume_one(entry)
    assert (await registry.get("window")).attempts == 1


@pytest.mark.asyncio
async def test_resume_request_exception_and_429_default():
    clock = FakeClock()
    cache = FakeCache()
    registry = LLMPauseRegistry(cache, clock=clock)
    entry = PauseEntry("doji", "ta-bot", "paused", 0, 0, 0)
    await registry.put(entry)
    audit = response(
        200,
        {
            "success": True,
            "data": [
                {
                    "changed_by": "petrosa-cio:owner",
                    "changed_at": datetime.fromtimestamp(0, UTC).isoformat(),
                }
            ],
        },
    )
    http = MagicMock(get=AsyncMock(return_value=audit))
    http.request = AsyncMock(side_effect=RuntimeError("down"))
    loop = build_loop(clock, cache, registry, http=http)
    await loop._resume_one(entry)
    assert (await registry.get("doji")).attempts == 1

    entry.attempts = 0
    entry.next_attempt_at = 0
    await registry.put(entry)
    cache.values.pop("cio:auto_resume:lock:doji", None)
    http.request = AsyncMock(return_value=response(429, {}))
    await loop._resume_one(entry)
    assert (await registry.get("doji")).next_attempt_at == 1300
