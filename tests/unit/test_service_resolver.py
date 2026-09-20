"""Unit tests for cio.core.service_resolver.TargetServiceResolver (petrosa-cio#200).

Covers:
- Display-name -> canonical-id resolution (the reported bug).
- Alias + normalized-id + already-canonical-id all resolving identically.
- Explicit ServiceType.UNKNOWN for genuinely unregistered ids (no more silent
  TA_BOT default).
- Regression: every currently-registered TA_BOT and REALTIME_STRATEGIES
  strategy id still resolves to its current ServiceType.
"""

import pytest

from cio.core.service_resolver import ServiceType, TargetServiceResolver


def test_resolve_display_name_returns_realtime_strategies():
    """AC1: resolve("Iceberg Order Detector") returns REALTIME_STRATEGIES."""
    assert (
        TargetServiceResolver.resolve("Iceberg Order Detector")
        == ServiceType.REALTIME_STRATEGIES
    )


def test_resolve_spread_liquidity_returns_realtime_strategies():
    """petrosa-cio#212: "spread_liquidity" must resolve, not UNKNOWN."""
    assert (
        TargetServiceResolver.resolve("spread_liquidity")
        == ServiceType.REALTIME_STRATEGIES
    )


@pytest.mark.parametrize(
    "strategy_id",
    [
        "spread_liquidity",
        "spread_liquidity_monitor",
        "Spread Liquidity Monitor",
        "  Spread   Liquidity   Monitor  ",
        "SPREAD_LIQUIDITY",
    ],
)
def test_resolve_all_spread_liquidity_variants_agree(strategy_id):
    """petrosa-cio#212: canonical id, alias, display name, and case/whitespace
    variants of the producer's "Spread Liquidity Monitor" strategy all
    resolve to the same ServiceType."""
    assert TargetServiceResolver.resolve(strategy_id) == ServiceType.REALTIME_STRATEGIES


@pytest.mark.parametrize(
    "strategy_id",
    [
        "iceberg_detector",
        "iceberg_order_detector",
        "Iceberg Order Detector",
        "  Iceberg   Order   Detector  ",
        "ICEBERG_DETECTOR",
    ],
)
def test_resolve_all_iceberg_variants_agree(strategy_id):
    """AC2: canonical id, alias, display name, and case/whitespace variants
    of each all resolve to the same ServiceType."""
    assert TargetServiceResolver.resolve(strategy_id) == ServiceType.REALTIME_STRATEGIES


@pytest.mark.parametrize(
    "strategy_id",
    [
        "totally_unknown_strategy",
        "Some Made Up Strategy Name",
        "",
        "   ",
    ],
)
def test_resolve_unknown_strategy_returns_unknown_not_ta_bot(strategy_id):
    """AC3: genuinely unknown ids return ServiceType.UNKNOWN, never TA_BOT."""
    result = TargetServiceResolver.resolve(strategy_id)
    assert result == ServiceType.UNKNOWN
    assert result != ServiceType.TA_BOT


def test_resolve_unknown_strategy_logs_warning(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        TargetServiceResolver.resolve("definitely_not_registered")
    assert "Unknown strategy_id" in caplog.text
    assert "ServiceType.UNKNOWN" in caplog.text


def test_regression_all_realtime_strategies_still_resolve():
    """AC4 (part 1): all 7 REALTIME_SERVICE_STRATEGIES entries still resolve
    to ServiceType.REALTIME_STRATEGIES (petrosa-cio#212 added "spread_liquidity",
    raising the count from 6 to 7)."""
    strategies = TargetServiceResolver.REALTIME_SERVICE_STRATEGIES
    assert len(strategies) == 7
    for strategy_id in strategies:
        assert (
            TargetServiceResolver.resolve(strategy_id)
            == ServiceType.REALTIME_STRATEGIES
        ), f"{strategy_id} no longer resolves to REALTIME_STRATEGIES"


def test_regression_all_ta_bot_strategies_still_resolve():
    """AC4 (part 2): all 27 TA_BOT_SERVICE_STRATEGIES entries still resolve
    to ServiceType.TA_BOT."""
    strategies = TargetServiceResolver.TA_BOT_SERVICE_STRATEGIES
    assert len(strategies) == 27
    for strategy_id in strategies:
        assert TargetServiceResolver.resolve(strategy_id) == ServiceType.TA_BOT, (
            f"{strategy_id} no longer resolves to ServiceType.TA_BOT"
        )


def test_normalize_collapses_whitespace_and_case():
    assert TargetServiceResolver._normalize("  Trade   Momentum  ") == "trade_momentum"
    assert TargetServiceResolver._normalize("trade_momentum") == "trade_momentum"


@pytest.mark.parametrize(
    ("strategy_id", "expected_canonical"),
    [
        ("Spread Liquidity Monitor", "spread_liquidity"),
        ("spread_liquidity_monitor", "spread_liquidity"),
        ("spread_liquidity", "spread_liquidity"),
        ("Iceberg Order Detector", "iceberg_detector"),
        ("trade_momentum", "trade_momentum"),
        ("  Trade   Momentum  ", "trade_momentum"),
        ("totally_unknown_strategy", "totally_unknown_strategy"),
        ("", ""),
    ],
)
def test_canonicalize_normalizes_and_aliases(strategy_id, expected_canonical):
    """petrosa-cio#212: ``canonicalize`` is the extracted normalize+alias
    chain ``resolve()`` uses internally, exposed for callers (e.g.
    ``ContextBuilder``) that need the canonical id itself rather than a
    ``ServiceType`` verdict."""
    assert TargetServiceResolver.canonicalize(strategy_id) == expected_canonical


def test_service_type_unknown_is_distinct_member():
    assert ServiceType.UNKNOWN != ServiceType.TA_BOT
    assert ServiceType.UNKNOWN != ServiceType.REALTIME_STRATEGIES
    assert ServiceType.UNKNOWN.value == "unknown"
