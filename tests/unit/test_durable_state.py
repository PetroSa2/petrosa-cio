import json

import httpx
import pytest

from cio.core.auto_resume import PauseEntry, migrate_redis_registry
from cio.core.durable_state import DataManagerStateStore


def entry_json(strategy_id: str) -> str:
    return PauseEntry(
        strategy_id,
        "ta-bot",
        "paused",
        1000.25,
        1001.5,
        1800,
        flap_count=1,
        attempts=2,
        next_attempt_at=2000.75,
    ).to_json()


@pytest.mark.asyncio
async def test_state_store_round_trips_and_lists_entries():
    requests: list[httpx.Request] = []
    payload = json.loads(entry_json("doji"))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/doji"):
            return httpx.Response(
                200, json={**payload, "updated_at": "2026-01-01T00:00:00Z"}
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"entries": [{**payload, "updated_at": "2026-01-01T00:00:00Z"}]},
            )
        return httpx.Response(200, json=payload)

    store = DataManagerStateStore("http://data-manager", "cio_auto_resume")
    await store._client.aclose()
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert json.loads(await store.get("doji")) == payload
    assert list(await store.all()) == ["doji"]
    await store.put("doji", entry_json("doji"))
    await store.delete("doji")
    assert requests[0].headers["X-Petrosa-Namespace"] == "cio_auto_resume"
    await store.close()


@pytest.mark.asyncio
async def test_state_store_missing_entry_returns_none():
    store = DataManagerStateStore("http://data-manager", "cio_auto_resume")
    await store._client.aclose()
    store._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    )
    assert await store.get("missing") is None
    await store.close()


class FakeRedis:
    def __init__(self, values: dict[str, str]):
        self.values = values
        self.deleted = False

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.values)

    async def delete(self, key: str) -> None:
        self.deleted = True


class FakeStore:
    def __init__(self, existing: dict[str, str] | None = None):
        self.values = existing or {}
        self.puts: list[str] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str) -> None:
        self.puts.append(key)
        self.values[key] = value


@pytest.mark.asyncio
async def test_migration_preserves_existing_and_deletes_legacy_hash(caplog):
    redis = FakeRedis({"doji": entry_json("doji"), "rsi": entry_json("rsi")})
    store = FakeStore({"doji": entry_json("doji")})
    await migrate_redis_registry(redis, store)
    assert store.puts == ["rsi"]
    assert redis.deleted is True
    assert "AUTO_RESUME_REGISTRY_MIGRATED count=1" in caplog.text


def test_state_store_default_timeout_tolerates_data_manager_tail(monkeypatch):
    monkeypatch.delenv("CIO_AUTO_RESUME_STORE_TIMEOUT_S", raising=False)
    store = DataManagerStateStore("http://data-manager", "cio_auto_resume")
    # Live data-manager p99 for the registry list was ~6.3s; 2s timed out ~13%.
    assert store.timeout.read == 10.0
    assert store.timeout.write == 10.0
    assert store.timeout.connect == 2.0
    assert store._client.timeout == store.timeout


def test_state_store_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv("CIO_AUTO_RESUME_STORE_TIMEOUT_S", "4.5")
    store = DataManagerStateStore("http://data-manager", "cio_auto_resume")
    assert store.timeout.read == 4.5
    assert store.timeout.connect == 2.0


def test_state_store_explicit_timeout_caps_connect_timeout():
    store = DataManagerStateStore("http://data-manager", "ns", timeout=1.0)
    assert store.timeout.read == 1.0
    assert store.timeout.connect == 1.0


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "  "])
def test_state_store_invalid_timeout_falls_back_to_default(monkeypatch, raw, caplog):
    monkeypatch.setenv("CIO_AUTO_RESUME_STORE_TIMEOUT_S", raw)
    store = DataManagerStateStore("http://data-manager", "cio_auto_resume")
    assert store.timeout.read == 10.0
    if raw.strip():
        assert "Invalid CIO_AUTO_RESUME_STORE_TIMEOUT_S" in caplog.text
