"""
Unit tests for #197: ContextBuilder ReadTimeout classification, ContextGap
recording, and the concurrent-timeout-storm consolidation.

Covers:
  - AC1: exception TYPE is logged (never just str(e), which is '' for
    httpx.ReadTimeout on httpx 0.28.1).
  - AC2: regime + strategy_stats + strategy_defaults timing out concurrently
    produces a single consolidated CONTEXT_FETCH_TIMEOUT_STORM summary line.
  - AC3: httpx.ReadTimeout is classified distinctly from generic Exception
    and logged at WARNING (not ERROR) with timeout value + endpoint.
  - AC4: degenerate StrategyStats fallback records
    ContextGap(surface='strategy_stats', ...), present in the emitted
    TriggerContext.pre_decision_context.gaps.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from cio.core.context_builder import ContextBuilder
from cio.models import ContextGap, PnlTrend, TriggerType


def _make_builder() -> ContextBuilder:
    return ContextBuilder(
        data_manager_url="http://dm",
        tradeengine_url="http://te",
    )


# ---------------------------------------------------------------------------
# AC1 + AC3: _fetch_regime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_regime_read_timeout_logs_warning_with_endpoint_and_timeout(
    caplog,
):
    caplog.set_level(logging.INFO, logger="cio.core.context_builder")
    builder = _make_builder()
    builder.client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))

    gaps: list[ContextGap] = []
    availability = {"market": True}
    result = await builder._fetch_regime(
        "BTCUSDT", "cid-1", gaps=gaps, availability=availability
    )

    # AC3: distinct WARNING (not the generic ERROR path) mentioning ReadTimeout
    # and the endpoint — never the misleading empty-tail "Failed to fetch regime: "
    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "FETCH_TIMEOUT" in r.message
    ]
    assert warning_records, "expected a FETCH_TIMEOUT WARNING record"
    body = warning_records[0].message
    assert "ReadTimeout" in body
    assert "http://dm/analysis/regime?pair=BTCUSDT" in body
    assert "timeout_s=" in body
    assert not any(
        r.levelno == logging.ERROR and "market" in r.message for r in caplog.records
    ), "ReadTimeout must not also fire the generic ERROR path"

    # Gap + availability recorded
    assert availability["market"] is False
    assert len(gaps) == 1
    assert gaps[0].surface == "market"
    assert gaps[0].reason.startswith("read_timeout")
    assert result.primary_signal == "timeout"

    await builder.close()


@pytest.mark.asyncio
async def test_fetch_regime_generic_exception_logs_exception_type_not_empty_tail(
    caplog,
):
    """AC1: a forced generic exception (simulating any non-ReadTimeout failure)
    must log the exception TYPE — the old code only logged str(e), which is
    empty for httpx.ReadTimeout and unhelpful for other exceptions too."""
    caplog.set_level(logging.INFO, logger="cio.core.context_builder")
    builder = _make_builder()

    class _WeirdEmptyStrException(Exception):
        def __str__(self):
            return ""

    builder.client.get = AsyncMock(side_effect=_WeirdEmptyStrException())

    gaps: list[ContextGap] = []
    result = await builder._fetch_regime("ETHUSDT", "cid-2", gaps=gaps)

    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "Failed to fetch regime" in r.message
    ]
    assert error_records
    body = error_records[0].message
    assert "exc_type=_WeirdEmptyStrException" in body
    assert body.strip() != "Failed to fetch regime:", (
        "must never regress to the empty-tail message"
    )
    assert gaps[0].reason.startswith("fetch_error exc_type=_WeirdEmptyStrException")
    assert "exc_type=_WeirdEmptyStrException" in result.thought_trace

    await builder.close()


# ---------------------------------------------------------------------------
# AC4: strategy_stats degenerate fallback records ContextGap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_strategy_stats_timeout_records_context_gap(caplog):
    caplog.set_level(logging.INFO, logger="cio.core.context_builder")
    builder = _make_builder()
    builder.client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))

    gaps: list[ContextGap] = []
    result = await builder._fetch_strategy_stats("strat-1", "cid-3", gaps=gaps)

    assert result.recent_pnl_trend == PnlTrend.NEUTRAL
    assert len(gaps) == 1
    assert gaps[0].surface == "strategy_stats"
    assert gaps[0].reason.startswith("read_timeout")

    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "FETCH_TIMEOUT" in r.message
    ]
    assert warning_records
    assert "surface=strategy_stats" in warning_records[0].message

    await builder.close()


@pytest.mark.asyncio
async def test_fetch_strategy_stats_without_gaps_collector_preserves_legacy_contract():
    """Existing direct-call test paths (no gaps kwarg) must keep working."""
    builder = _make_builder()
    builder.client.get = AsyncMock(side_effect=RuntimeError("boom"))

    result = await builder._fetch_strategy_stats("strat-2", "cid-4")
    assert result.recent_pnl_trend == PnlTrend.NEUTRAL

    await builder.close()


# ---------------------------------------------------------------------------
# #209 root cause: a 200 response with structurally-absent required fields
# must be gap-tracked the same as an exception, not silently accepted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_strategy_stats_success_with_structural_none_fields_records_gap(
    caplog,
):
    """#209 (AC1/AC2 root cause): data-manager's /analysis/performance
    endpoint returns HTTP 200 but never populates win_rate_delta or
    consecutive_losses (confirmed by inspection of
    data_manager/api/routes/analysis.py::get_strategy_performance). Because
    strategy_assessor.REQUIRED_CONTEXT_FIELDS mandates both, every call
    self-reports MISSING_INPUT regardless of whether the strategy has real
    trading history. This was previously invisible — the old code only
    gap-tracked the *exception* path. A successful response carrying these
    structurally-absent fields must now also produce a strategy_stats gap."""
    caplog.set_level(logging.WARNING, logger="cio.core.context_builder")
    builder = _make_builder()
    builder.client.get = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {
                "stats": {
                    "win_rate": 0.55,
                    "win_rate_delta": None,
                    "consecutive_losses": None,
                    "recent_pnl_trend": "positive",
                }
            },
        )
    )

    gaps: list[ContextGap] = []
    result = await builder._fetch_strategy_stats("strat-real-3", "cid-5", gaps=gaps)

    assert result.win_rate == 0.55
    assert result.win_rate_delta is None
    assert result.consecutive_losses is None

    stats_gaps = [g for g in gaps if g.surface == "strategy_stats"]
    assert stats_gaps
    assert stats_gaps[0].reason.startswith("structural_gap:")
    assert "win_rate_delta" in stats_gaps[0].reason
    assert "consecutive_losses" in stats_gaps[0].reason

    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "STRATEGY_STATS_STRUCTURAL_GAP" in r.message
    ]
    assert warning_records
    assert "strat-real-3" in warning_records[0].message

    await builder.close()


@pytest.mark.asyncio
async def test_fetch_strategy_stats_success_with_complete_fields_records_no_gap():
    """AC2: a fully-populated response (all REQUIRED_CONTEXT_FIELDS present)
    must not record a spurious structural gap."""
    builder = _make_builder()
    builder.client.get = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {
                "stats": {
                    "win_rate": 0.6,
                    "win_rate_delta": 0.05,
                    "consecutive_losses": 1,
                    "recent_pnl_trend": "positive",
                }
            },
        )
    )

    gaps: list[ContextGap] = []
    result = await builder._fetch_strategy_stats("strat-real-4", "cid-6", gaps=gaps)

    assert result.win_rate_delta == 0.05
    assert result.consecutive_losses == 1
    assert not [g for g in gaps if g.surface == "strategy_stats"]

    await builder.close()


# ---------------------------------------------------------------------------
# AC2: concurrent all-fail produces one consolidated summary line
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_emits_consolidated_timeout_storm_on_concurrent_failure(caplog):
    caplog.set_level(logging.INFO, logger="cio.core.context_builder")
    builder = _make_builder()

    # Every data-manager call (regime, strategy stats, strategy defaults) times
    # out; tradeengine call for portfolio/risk fails with a plain exception
    # (unrelated surface, must not be counted in the timeout storm).
    async def _get(url, *args, **kwargs):
        if "data-manager" in url or "dm" in url.split("/")[2]:
            raise httpx.ReadTimeout("")
        raise RuntimeError("tradeengine down")

    builder.client.get = _get

    ctx = await builder.build(
        correlation_id="cid-storm",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={"symbol": "BTCUSDT", "strategy_id": "strat-storm"},
    )

    storm_records = [
        r for r in caplog.records if "CONTEXT_FETCH_TIMEOUT_STORM" in r.message
    ]
    assert storm_records, "expected a consolidated CONTEXT_FETCH_TIMEOUT_STORM line"
    body = storm_records[0].message
    assert "market" in body
    assert "strategy_stats" in body
    assert "strategy_defaults" in body
    assert "count=3" in body

    # AC4: the gap is present in the emitted TriggerContext.
    gap_surfaces = {g.surface for g in ctx.pre_decision_context.gaps}
    assert {"market", "strategy_stats", "strategy_defaults"} <= gap_surfaces

    await builder.close()


@pytest.mark.asyncio
async def test_build_does_not_emit_storm_summary_for_single_surface_timeout(caplog):
    """A single-surface timeout must not trigger the consolidated summary —
    only the per-surface WARNING line (avoids noisy false-correlation)."""
    caplog.set_level(logging.INFO, logger="cio.core.context_builder")
    builder = _make_builder()

    async def _get(url, *args, **kwargs):
        if "/analysis/regime" in url:
            raise httpx.ReadTimeout("")
        if "/analysis/performance" in url:
            return MagicMock(
                status_code=200,
                json=lambda: {"stats": {"recent_pnl_trend": "neutral"}},
                raise_for_status=lambda: None,
            )
        if "/api/v1/config/strategies" in url:
            return MagicMock(
                status_code=200,
                json=lambda: {"parameters": {}},
                raise_for_status=lambda: None,
            )
        return MagicMock(
            status_code=200,
            json=lambda: {
                "portfolio": {
                    "gross_exposure": 0.0,
                    "same_asset_pct": 0.0,
                    "open_positions_count": 0,
                },
                "risk_limits": {
                    "max_drawdown_pct": 0.1,
                    "max_orders_global": 50,
                    "max_orders_per_symbol": 5,
                    "max_position_size_usd": 1000.0,
                    "max_position_size_pct": 0.5,
                    "volatility_scale_threshold": 0.5,
                },
                "env_stats": {},
            },
            raise_for_status=lambda: None,
        )

    builder.client.get = _get

    await builder.build(
        correlation_id="cid-single",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={"symbol": "BTCUSDT", "strategy_id": "strat-single"},
    )

    storm_records = [
        r for r in caplog.records if "CONTEXT_FETCH_TIMEOUT_STORM" in r.message
    ]
    assert not storm_records

    await builder.close()
