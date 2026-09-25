"""
Unit tests for #236: ContextBuilder._fetch_portfolio_and_risk connect-error
retry with short backoff.

petrosa-cio#236's incident log showed:
  "Failed to fetch portfolio/risk: All connection attempts failed"
— httpx.ConnectError, a fast TCP-level failure. Previously a single failed
attempt fell straight through to the conservative safe-default fallback
(gross_exposure=1.0, orders=999 — a trigger-blocking response) even for a
one-off transient blip. This adds a short, bounded retry scoped to
httpx.ConnectError specifically (not httpx.ReadTimeout — #199 already
trimmed the read timeout to keep the decision window tight, and retrying a
slow timeout would reintroduce that burn).

Covers:
  - A transient ConnectError that clears within the retry budget returns the
    real portfolio/risk data (no gap recorded, no fallback defaults).
  - A persistent ConnectError across all attempts still falls back to the
    existing conservative defaults, with the existing gap/availability
    contract unchanged, after exhausting the retry budget (not on the first
    failure).
  - A non-ConnectError exception (e.g. httpx.ReadTimeout, a generic
    exception) is NOT retried — single attempt only, preserving #199's
    timeout-budget precedent.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from cio.core import context_builder as context_builder_module
from cio.core.context_builder import ContextBuilder
from cio.models import ContextGap

_PORTFOLIO_SUCCESS_PAYLOAD = {
    "portfolio": {
        "gross_exposure": 0.2,
        "same_asset_pct": 0.1,
        "open_positions_count": 2,
    },
    "risk_limits": {
        "max_drawdown_pct": 0.1,
        "max_orders_global": 50,
        "max_orders_per_symbol": 5,
        "max_position_size_usd": 1000.0,
    },
    "env_stats": {"available_capital_usd": 5000.0},
}


def _make_builder(clock=None) -> ContextBuilder:
    return ContextBuilder(
        data_manager_url="http://dm",
        tradeengine_url="http://te",
        clock=clock,
    )


def _success_response():
    from unittest.mock import MagicMock

    return MagicMock(
        raise_for_status=lambda: None,
        json=lambda: _PORTFOLIO_SUCCESS_PAYLOAD,
    )


@pytest.mark.asyncio
async def test_transient_connect_error_recovers_within_retry_budget():
    builder = _make_builder()
    builder.client.get = AsyncMock(
        side_effect=[
            httpx.ConnectError("All connection attempts failed"),
            _success_response(),
        ]
    )

    gaps: list[ContextGap] = []
    availability = {"portfolio": True}
    portfolio, risk, env_stats = await builder._fetch_portfolio_and_risk(
        "BTCUSDT", "cid-1", gaps=gaps, availability=availability
    )

    assert portfolio.gross_exposure == 0.2
    assert portfolio.open_positions_count == 2
    assert risk.max_orders_global == 50
    assert env_stats["available_capital_usd"] == 5000.0
    assert not gaps, "a recovered transient failure must not record a gap"
    assert availability["portfolio"] is True
    assert builder.client.get.await_count == 2

    await builder.close()


@pytest.mark.asyncio
async def test_persistent_connect_error_exhausts_retries_then_falls_back(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="cio.core.context_builder")
    builder = _make_builder()
    builder.client.get = AsyncMock(
        side_effect=httpx.ConnectError("All connection attempts failed")
    )

    gaps: list[ContextGap] = []
    availability = {"portfolio": True}
    portfolio, risk, env_stats = await builder._fetch_portfolio_and_risk(
        "BTCUSDT", "cid-2", gaps=gaps, availability=availability
    )

    # Existing safe-default contract (P1.4-AC2, #132) is unchanged.
    assert portfolio.gross_exposure == 1.0
    assert portfolio.open_positions_count == 999
    assert risk.max_orders_global == 0
    assert env_stats["open_orders_global"] == 999
    assert len(gaps) == 1
    assert gaps[0].surface == "portfolio"
    assert gaps[0].reason.startswith("fetch_error:")
    assert availability["portfolio"] is False

    # Default budget is 2 retries -> 3 total attempts.
    assert builder.client.get.await_count == 3

    retry_records = [
        r for r in caplog.records if "PORTFOLIO_FETCH_CONNECT_RETRY" in r.message
    ]
    assert len(retry_records) == 2, "expected one retry log per retried attempt"

    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "Failed to fetch portfolio/risk" in r.message
    ]
    assert error_records, "final exhaustion must still log the existing ERROR line"

    await builder.close()


@pytest.mark.asyncio
async def test_read_timeout_is_not_retried():
    """A slow failure (ReadTimeout) must fall straight to the existing
    fallback in a single attempt — retrying it would reintroduce the
    decision-window burn #199 fixed."""
    builder = _make_builder()
    builder.client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))

    gaps: list[ContextGap] = []
    portfolio, risk, env_stats = await builder._fetch_portfolio_and_risk(
        "BTCUSDT", "cid-3", gaps=gaps
    )

    assert portfolio.open_positions_count == 999
    assert len(gaps) == 1
    assert builder.client.get.await_count == 1, (
        "ReadTimeout must not be retried, only httpx.ConnectError"
    )

    await builder.close()


@pytest.mark.asyncio
async def test_generic_exception_is_not_retried():
    builder = _make_builder()
    builder.client.get = AsyncMock(side_effect=RuntimeError("boom"))

    gaps: list[ContextGap] = []
    portfolio, _risk, _env_stats = await builder._fetch_portfolio_and_risk(
        "BTCUSDT", "cid-4", gaps=gaps
    )

    assert portfolio.open_positions_count == 999
    assert builder.client.get.await_count == 1

    await builder.close()


@pytest.mark.asyncio
async def test_without_gaps_collector_preserves_legacy_contract():
    """Existing direct-call test paths (no gaps kwarg) must keep working."""
    builder = _make_builder()
    builder.client.get = AsyncMock(
        side_effect=httpx.ConnectError("All connection attempts failed")
    )

    portfolio, risk, env_stats = await builder._fetch_portfolio_and_risk(
        "BTCUSDT", "cid-5"
    )
    assert portfolio.open_positions_count == 999
    assert risk.max_orders_global == 0
    assert env_stats["open_orders_global"] == 999

    await builder.close()


@pytest.mark.asyncio
async def test_recent_cache_is_used_after_connect_error(monkeypatch, caplog):
    monkeypatch.setattr(context_builder_module, "_PORTFOLIO_FETCH_MAX_RETRIES", 0)
    now = [100.0]
    builder = _make_builder(clock=lambda: now[0])
    builder.client.get = AsyncMock(
        side_effect=[
            _success_response(),
            httpx.ConnectError("All connection attempts failed"),
        ]
    )

    await builder._fetch_portfolio_and_risk("BTCUSDT", "cid-cache-1")
    now[0] = 101.5
    gaps: list[ContextGap] = []
    availability = {"portfolio": True}
    portfolio, risk, env_stats = await builder._fetch_portfolio_and_risk(
        "BTCUSDT", "cid-cache-2", gaps=gaps, availability=availability
    )

    assert portfolio.gross_exposure == 0.2
    assert risk.max_orders_global == 50
    assert env_stats["available_capital_usd"] == 5000.0
    assert [gap.reason for gap in gaps] == ["portfolio_state_stale_cache"]
    assert availability["portfolio"] is False
    assert any(
        "PORTFOLIO_FETCH_STALE_CACHE_USED age_s=1.500" in r.message
        for r in caplog.records
    )

    await builder.close()


@pytest.mark.asyncio
async def test_expired_cache_keeps_fail_closed_fallback(monkeypatch):
    monkeypatch.setattr(context_builder_module, "_PORTFOLIO_FETCH_MAX_RETRIES", 0)
    now = [100.0]
    builder = _make_builder(clock=lambda: now[0])
    builder.client.get = AsyncMock(
        side_effect=[
            _success_response(),
            httpx.ConnectError("All connection attempts failed"),
        ]
    )

    await builder._fetch_portfolio_and_risk("BTCUSDT", "cid-expired-1")
    now[0] = 220.0
    portfolio, risk, env_stats = await builder._fetch_portfolio_and_risk(
        "BTCUSDT", "cid-expired-2"
    )

    assert portfolio.gross_exposure == 1.0
    assert portfolio.open_positions_count == 999
    assert risk.max_orders_global == 0
    assert env_stats["open_orders_global"] == 999

    await builder.close()


@pytest.mark.asyncio
async def test_cache_is_scoped_per_symbol(monkeypatch):
    monkeypatch.setattr(context_builder_module, "_PORTFOLIO_FETCH_MAX_RETRIES", 0)
    now = [100.0]
    builder = _make_builder(clock=lambda: now[0])
    builder.client.get = AsyncMock(
        side_effect=[
            _success_response(),
            httpx.ConnectError("All connection attempts failed"),
        ]
    )

    await builder._fetch_portfolio_and_risk("BTCUSDT", "cid-symbol-1")
    now[0] = 101.0
    portfolio, _risk, env_stats = await builder._fetch_portfolio_and_risk(
        "ETHUSDT", "cid-symbol-2"
    )

    assert portfolio.gross_exposure == 1.0
    assert portfolio.open_positions_count == 999
    assert env_stats["open_orders_global"] == 999

    await builder.close()
