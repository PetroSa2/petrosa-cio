"""Tests for :mod:`cio.core.envelope_fetcher` (P4.6-AC3, #154, FR62)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cio.apps import envelopes_api
from cio.core.envelope_fetcher import (
    ACTIVE_ENVELOPE_PATH,
    EnvelopeFetcher,
    EnvelopeFetchError,
    EnvelopeNotFoundError,
)


def _envelope(
    *,
    envelope_id: str = "env-1",
    version: int = 5,
    key: str = "strategy:momentum-v3",
    source: str = "characterization",
    value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "envelope_id": envelope_id,
        "version": version,
        "strategy_or_portfolio_key": key,
        "value": value or {"max_drawdown_pct": 12.0},
        "source": source,
        "originating_characterization_revision": "rev-abc",
        "operator_id": "alice" if source == "operator_approved" else None,
        "created_at": "2026-05-30T22:00:00Z",
        "signed_action_id": "sa-1",
    }


def _make_fetcher(handler, **kwargs) -> EnvelopeFetcher:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return EnvelopeFetcher(
        data_manager_url="http://dm.local",
        client=client,
        **kwargs,
    )


# ─── happy path ──────────────────────────────────────────────────────────────


def test_get_active_returns_envelope_from_data_manager() -> None:
    call_count = {"n": 0}
    expected_url = "http://dm.local" + ACTIVE_ENVELOPE_PATH + "strategy:momentum-v3"

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        assert str(request.url) == expected_url
        return httpx.Response(200, json=_envelope())

    fetcher = _make_fetcher(handler)
    env = asyncio.run(fetcher.get_active("strategy:momentum-v3"))
    assert env["envelope_id"] == "env-1"
    assert env["version"] == 5
    assert call_count["n"] == 1
    asyncio.run(fetcher.aclose())


# ─── TTL cache ───────────────────────────────────────────────────────────────


def test_consecutive_gets_within_ttl_coalesce_to_one_upstream_call() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=_envelope())

    fetcher = _make_fetcher(handler, ttl_seconds=60.0)

    async def run() -> None:
        a = await fetcher.get_active("strategy:momentum-v3")
        b = await fetcher.get_active("strategy:momentum-v3")
        assert a == b
        await fetcher.aclose()

    asyncio.run(run())
    assert call_count["n"] == 1


def test_invalidate_forces_refetch() -> None:
    call_count = {"n": 0}
    versions = [5, 6]

    def handler(request: httpx.Request) -> httpx.Response:
        v = versions[call_count["n"]]
        call_count["n"] += 1
        return httpx.Response(200, json=_envelope(version=v))

    fetcher = _make_fetcher(handler, ttl_seconds=60.0)

    async def run() -> None:
        first = await fetcher.get_active("strategy:momentum-v3")
        assert first["version"] == 5
        fetcher.invalidate("strategy:momentum-v3")
        second = await fetcher.get_active("strategy:momentum-v3")
        assert second["version"] == 6
        await fetcher.aclose()

    asyncio.run(run())
    assert call_count["n"] == 2


def test_ttl_expiry_triggers_refetch() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=_envelope(version=call_count["n"]))

    fetcher = _make_fetcher(handler, ttl_seconds=0.01)

    async def run() -> None:
        first = await fetcher.get_active("strategy:momentum-v3")
        assert first["version"] == 1
        await asyncio.sleep(0.05)
        second = await fetcher.get_active("strategy:momentum-v3")
        assert second["version"] == 2
        await fetcher.aclose()

    asyncio.run(run())
    assert call_count["n"] == 2


# ─── AC3.c — precedence by version (no source filter) ─────────────────────────


def test_helper_returns_whatever_dm_serves_regardless_of_source() -> None:
    """AC3.c regression guard: helper MUST NOT filter by ``source``.

    Setup: data-manager returns a characterization v=5 (because it's the
    highest version, even though an older operator_approved v=4 exists in
    the store). The helper must surface that v=5 — if anyone reintroduces
    "prefer operator_approved" filtering on the cio side, this test fails.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_envelope(version=5, source="characterization"),
        )

    fetcher = _make_fetcher(handler)
    env = asyncio.run(fetcher.get_active("strategy:momentum-v3"))
    assert env["version"] == 5
    assert env["source"] == "characterization"
    asyncio.run(fetcher.aclose())


# ─── AC3.b — fallback / refusal ─────────────────────────────────────────────


def test_404_raises_envelope_not_found_for_admission_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"detail": {"title": "Envelope not found"}},
        )

    fetcher = _make_fetcher(handler)
    with pytest.raises(EnvelopeNotFoundError) as exc_info:
        asyncio.run(fetcher.get_active("strategy:unknown"))
    assert "strategy:unknown" in str(exc_info.value)
    asyncio.run(fetcher.aclose())


def test_5xx_raises_fetch_error_distinct_from_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "mongo down"})

    fetcher = _make_fetcher(handler)
    with pytest.raises(EnvelopeFetchError) as exc_info:
        asyncio.run(fetcher.get_active("strategy:any"))
    assert "503" in str(exc_info.value)
    asyncio.run(fetcher.aclose())


def test_transport_error_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fetcher = _make_fetcher(handler)
    with pytest.raises(EnvelopeFetchError) as exc_info:
        asyncio.run(fetcher.get_active("strategy:any"))
    assert "transport" in str(exc_info.value)
    asyncio.run(fetcher.aclose())


def test_empty_key_raises_value_error() -> None:
    fetcher = _make_fetcher(lambda r: httpx.Response(200, json=_envelope()))
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(fetcher.get_active(""))
    assert "non-empty" in str(exc_info.value)
    asyncio.run(fetcher.aclose())


# ─── AC3.f — /healthz/envelopes endpoint ────────────────────────────────────


def test_healthz_envelopes_reports_cache_snapshot_after_fetch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_envelope(version=7, source="operator_approved")
        )

    fetcher = _make_fetcher(handler, ttl_seconds=60.0)
    asyncio.run(fetcher.get_active("strategy:momentum-v3"))

    app = FastAPI()
    app.state.envelope_fetcher = fetcher
    app.include_router(envelopes_api.router)
    client = TestClient(app)
    response = client.get("/healthz/envelopes")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["count"] == 1
    entry = body["entries"]["strategy:momentum-v3"]
    assert entry["version"] == 7
    assert entry["source"] == "operator_approved"
    assert entry["fresh"] is True
    asyncio.run(fetcher.aclose())


def test_healthz_envelopes_reports_unwired_when_no_fetcher() -> None:
    app = FastAPI()
    app.include_router(envelopes_api.router)
    client = TestClient(app)
    response = client.get("/healthz/envelopes")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unwired"
    assert body["entries"] == {}
