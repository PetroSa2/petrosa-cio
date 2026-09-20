"""Unit tests for petrosa-cio#212: ContextBuilder must normalize the raw
payload ``strategy_id`` before it is used to build ``context.strategy_id``
and before the downstream fetches keyed on it.

Root cause: ``context_builder.py:142`` read ``payload["strategy_id"]``
verbatim. Producers sometimes emit a human display name (e.g. "Spread
Liquidity Monitor" instead of "spread_liquidity") in that field. Routing
already normalized (``router.py:_resolve_routing_strategy_id`` /
``TargetServiceResolver``); the context path did not, so a display-name id
silently missed data-manager's
``/analysis/performance/{strategy_id}`` endpoint, producing empty stats —
one of the two feeds into the ``pause_strategy``-every-cycle bias (the
other being petrosa-data-manager#318).
"""

from unittest.mock import MagicMock

import pytest

from cio.core.context_builder import ContextBuilder
from cio.models import TriggerType


def _make_builder() -> ContextBuilder:
    return ContextBuilder(
        data_manager_url="http://dm",
        tradeengine_url="http://te",
    )


def _ok(json_body: dict) -> MagicMock:
    return MagicMock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: json_body,
    )


def _default_router(urls_seen: list[str]):
    """Routes every ContextBuilder.build() HTTP call to a minimally valid
    response, recording every URL requested so tests can assert on the
    exact strategy_id that landed in each one."""

    async def _get(url, *args, **kwargs):
        urls_seen.append(url)
        if "/analysis/regime" in url:
            return _ok(
                {
                    "regime": "trending",
                    "regime_confidence": "high",
                    "volatility_level": "medium",
                    "primary_signal": "momentum",
                }
            )
        if "/analysis/performance/" in url:
            return _ok({"stats": {"recent_pnl_trend": "neutral"}})
        if "/api/v1/config/strategies/" in url:
            return _ok({"parameters": {}})
        # tradeengine portfolio/risk state endpoint
        return _ok(
            {
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
            }
        )

    return _get


@pytest.mark.asyncio
async def test_build_normalizes_display_name_strategy_id_to_canonical():
    """AC: a payload with strategy_id="Spread Liquidity Monitor" must
    produce context.strategy_id == "spread_liquidity", and the performance
    URL must be built with the canonical id — never the display name."""
    builder = _make_builder()
    urls_seen: list[str] = []
    builder.client.get = _default_router(urls_seen)

    ctx = await builder.build(
        correlation_id="cid-212-a",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={"symbol": "BTCUSDT", "strategy_id": "Spread Liquidity Monitor"},
    )

    assert ctx.strategy_id == "spread_liquidity"

    performance_urls = [u for u in urls_seen if "/analysis/performance/" in u]
    assert performance_urls, "expected a performance-stats fetch"
    assert performance_urls[0].endswith("/analysis/performance/spread_liquidity")
    assert "Spread" not in performance_urls[0]
    assert " " not in performance_urls[0]

    defaults_urls = [u for u in urls_seen if "/api/v1/config/strategies/" in u]
    assert defaults_urls
    assert defaults_urls[0].endswith("/api/v1/config/strategies/spread_liquidity")

    await builder.close()


@pytest.mark.asyncio
async def test_build_leaves_already_canonical_strategy_id_unchanged():
    """Regression: an already-canonical snake_case id (the common case,
    matching existing CIO test fixtures like "momentum_pulse") must pass
    through unchanged — normalization must be a no-op for it."""
    builder = _make_builder()
    urls_seen: list[str] = []
    builder.client.get = _default_router(urls_seen)

    ctx = await builder.build(
        correlation_id="cid-212-b",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={"symbol": "BTCUSDT", "strategy_id": "momentum_pulse"},
    )

    assert ctx.strategy_id == "momentum_pulse"
    performance_urls = [u for u in urls_seen if "/analysis/performance/" in u]
    assert performance_urls[0].endswith("/analysis/performance/momentum_pulse")

    await builder.close()


@pytest.mark.asyncio
async def test_build_normalizes_iceberg_display_name_regression():
    """Regression: the pre-existing "Iceberg Order Detector" display-name
    case (petrosa-cio#200's alias) must still normalize through the same
    context_builder.py:142 code path."""
    builder = _make_builder()
    urls_seen: list[str] = []
    builder.client.get = _default_router(urls_seen)

    ctx = await builder.build(
        correlation_id="cid-212-c",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={"symbol": "BTCUSDT", "strategy_id": "Iceberg Order Detector"},
    )

    assert ctx.strategy_id == "iceberg_detector"

    await builder.close()
