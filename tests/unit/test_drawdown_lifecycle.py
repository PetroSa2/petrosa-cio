import json

import pytest

from cio.core.context_builder import ContextBuilder
from cio.core.drawdown_lifecycle import KEY, DrawdownLifecycleStore
from cio.core.router import OutputRouter


class FakeStore:
    def __init__(self, value=None):
        self.value = value
        self.calls = []

    async def get(self, key):
        self.calls.append(("get", key))
        return self.value

    async def put(self, key, value):
        self.calls.append(("put", key, value))
        self.value = value


class FailingStore(FakeStore):
    async def put(self, key, value):
        raise RuntimeError("store unavailable")


class Decision:
    def __init__(self, event, started, resumed=None, threshold=None):
        self.lifecycle_event = event
        self.halt_started_at = started
        self.resumed_drawdown = resumed
        self.resume_threshold = threshold


class RecordingStore:
    def __init__(self, failure=False, value=None):
        self.saved = []
        self.failure = failure
        self.value = value

    async def load(self):
        if self.failure:
            raise RuntimeError("store unavailable")
        return self.value

    async def save(self, decision):
        if self.failure:
            raise RuntimeError("store unavailable")
        self.saved.append(decision)


@pytest.mark.asyncio
async def test_rehalt_overwrites_the_previous_episode():
    store_backend = FakeStore()
    store = DrawdownLifecycleStore(store_backend)

    await store.save(Decision("halt", 10.0))
    await store.save(Decision("resume", 10.0, 0.05, 0.061))
    await store.save(Decision("rehalt", 20.0))

    assert await store.load() == {
        "halt_started_at": 20.0,
        "resumed_drawdown": None,
        "resume_threshold": None,
    }
    assert any(call[0:2] == ("put", KEY) for call in store_backend.calls)


@pytest.mark.asyncio
async def test_missing_or_malformed_record_is_ignored():
    cache = FakeStore()

    assert await DrawdownLifecycleStore(cache).load() is None

    cache.value = json.dumps({"gave_up_reason": json.dumps({"halt_started_at": 10.0})})

    assert await DrawdownLifecycleStore(cache).load() is None

    cache.value = "not-json"
    assert await DrawdownLifecycleStore(cache).load() is None

    cache.value = json.dumps(
        {"gave_up_reason": json.dumps({
            "halt_started_at": "20",
            "resumed_drawdown": None,
            "resume_threshold": None,
        })}
    )
    assert await DrawdownLifecycleStore(cache).load() is None


@pytest.mark.asyncio
async def test_non_lifecycle_decision_does_not_replace_record():
    store_backend = FakeStore()
    store = DrawdownLifecycleStore(store_backend)

    await store.save(Decision(None, 99.0))

    assert store_backend.value is None


@pytest.mark.asyncio
async def test_store_write_failure_does_not_change_the_decision_path():
    store = DrawdownLifecycleStore(FailingStore())

    with pytest.raises(RuntimeError):
        await store.save(Decision("rehalt", 20.0))


def test_latest_lifecycle_overlays_only_lifecycle_fields():
    raw = {"from_peak_pct": 0.07, "halt_started_at": 10.0}
    persisted = {
        "halt_started_at": 20.0,
        "resumed_drawdown": None,
        "resume_threshold": None,
    }

    assert ContextBuilder._merge_drawdown_lifecycle(raw, persisted) == {
        "from_peak_pct": 0.07,
        "halt_started_at": 20.0,
        "resumed_drawdown": None,
        "resume_threshold": None,
    }


@pytest.mark.asyncio
async def test_router_persists_lifecycle_without_blocking_on_store_failure():
    router = OutputRouter.__new__(OutputRouter)
    store = RecordingStore()
    router.drawdown_lifecycle_store = store
    decision = Decision("rehalt", 20.0)

    await router._persist_drawdown_lifecycle(decision, "corr-1")

    assert store.saved == [decision]

    router.drawdown_lifecycle_store = RecordingStore(failure=True)
    await router._persist_drawdown_lifecycle(decision, "corr-2")


@pytest.mark.asyncio
async def test_context_loads_lifecycle_and_ignores_store_failure():
    builder = ContextBuilder.__new__(ContextBuilder)
    lifecycle = {
        "halt_started_at": 20.0,
        "resumed_drawdown": None,
        "resume_threshold": None,
    }
    builder.drawdown_lifecycle_store = RecordingStore(value=lifecycle)

    assert await builder._load_drawdown_lifecycle() == lifecycle

    builder.drawdown_lifecycle_store = RecordingStore(failure=True)
    assert await builder._load_drawdown_lifecycle() is None

    builder.drawdown_lifecycle_store = None
    assert await builder._load_drawdown_lifecycle() is None
