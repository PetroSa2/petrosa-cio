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
    """AC4 (part 1): all 5 REALTIME_SERVICE_STRATEGIES entries still resolve
    to ServiceType.REALTIME_STRATEGIES (petrosa-cio#214 reconciled the set
    against the producer's actual enabled ids: removed "orderbook_skew",
    "trade_momentum", "ticker_velocity" (never emitted by the producer) and
    added "cross_exchange_spread" (enabled by default), raising the count
    from 7 non-matching entries to 5 accurate ones)."""
    strategies = TargetServiceResolver.REALTIME_SERVICE_STRATEGIES
    assert len(strategies) == 5
    for strategy_id in strategies:
        assert (
            TargetServiceResolver.resolve(strategy_id)
            == ServiceType.REALTIME_STRATEGIES
        ), f"{strategy_id} no longer resolves to REALTIME_STRATEGIES"


def test_regression_all_ta_bot_strategies_still_resolve():
    """AC4 (part 2): all 28 TA_BOT_SERVICE_STRATEGIES entries still resolve
    to ServiceType.TA_BOT (petrosa-cio#214 added "order_flow_imbalance",
    raising the count from 27 to 28)."""
    strategies = TargetServiceResolver.TA_BOT_SERVICE_STRATEGIES
    assert len(strategies) == 28
    for strategy_id in strategies:
        assert TargetServiceResolver.resolve(strategy_id) == ServiceType.TA_BOT, (
            f"{strategy_id} no longer resolves to ServiceType.TA_BOT"
        )


def test_cross_exchange_spread_resolves_realtime_strategies():
    """petrosa-cio#214: "cross_exchange_spread" is enabled by default in the
    producer (petrosa-realtime-strategies/constants.py) but was previously
    absent from the registry entirely — resolved ServiceType.UNKNOWN."""
    assert (
        TargetServiceResolver.resolve("cross_exchange_spread")
        == ServiceType.REALTIME_STRATEGIES
    )


def test_order_flow_imbalance_resolves_ta_bot():
    """petrosa-cio#214: "order_flow_imbalance" is registered by the producer
    (ta_bot/config.py) but was the only one of 28 missing here, resolving
    ServiceType.UNKNOWN and leaving the strategy unroutable for
    MODIFY_PARAMS/FAIL_SAFE while still trading via EXECUTE."""
    assert TargetServiceResolver.resolve("order_flow_imbalance") == ServiceType.TA_BOT


@pytest.mark.parametrize(
    "strategy_id", ["orderbook_skew", "trade_momentum", "ticker_velocity"]
)
def test_nonexistent_realtime_strategies_now_resolve_unknown(strategy_id):
    """petrosa-cio#214: these three ids do not exist in the producer
    (petrosa-realtime-strategies) at all — no STRATEGY_ENABLED_* flag, no
    strategy module. They must resolve UNKNOWN, not a phantom service."""
    assert TargetServiceResolver.resolve(strategy_id) == ServiceType.UNKNOWN


def test_every_producer_strategy_id_resolves_to_a_known_service_type():
    """petrosa-cio#214 acceptance criterion: a test asserts every strategy
    id a producer can emit resolves to a known ServiceType, so the next
    unregistered strategy fails CI instead of trading unsupervised.

    This mirrors the two producers' full strategy catalogs verbatim:
    - petrosa-bot-ta-analysis/ta_bot/config.py `enabled_strategies` (28 ids).
    - petrosa-realtime-strategies/constants.py `get_enabled_strategies()`
      possible outputs (5 ids gated by STRATEGY_ENABLED_* flags, including
      "onchain_metrics" which defaults to disabled but is still a valid,
      routable producer id).
    """
    ta_bot_producer_ids = {
        "momentum_pulse",
        "band_fade_reversal",
        "golden_trend_sync",
        "range_break_pop",
        "divergence_trap",
        "volume_surge_breakout",
        "mean_reversion_scalper",
        "ichimoku_cloud_momentum",
        "liquidity_grab_reversal",
        "multi_timeframe_trend_continuation",
        "order_flow_imbalance",
        "ema_alignment_bullish",
        "bollinger_squeeze_alert",
        "bollinger_breakout_signals",
        "rsi_extreme_reversal",
        "inside_bar_breakout",
        "ema_pullback_continuation",
        "ema_momentum_reversal",
        "fox_trap_reversal",
        "hammer_reversal_pattern",
        "bear_trap_buy",
        "inside_bar_sell",
        "shooting_star_reversal",
        "doji_reversal",
        "ema_alignment_bearish",
        "ema_slope_reversal_sell",
        "minervini_trend_template",
        "bear_trap_sell",
    }
    assert len(ta_bot_producer_ids) == 28
    for strategy_id in ta_bot_producer_ids:
        assert TargetServiceResolver.resolve(strategy_id) == ServiceType.TA_BOT, (
            f"producer id {strategy_id!r} does not resolve to a known "
            "ServiceType — the TA-bot registry has drifted from the "
            "producer's enabled_strategies list"
        )

    realtime_producer_ids = {
        "btc_dominance",
        "cross_exchange_spread",
        "onchain_metrics",
        "spread_liquidity",
        "iceberg_detector",
    }
    assert len(realtime_producer_ids) == 5
    for strategy_id in realtime_producer_ids:
        assert (
            TargetServiceResolver.resolve(strategy_id)
            == ServiceType.REALTIME_STRATEGIES
        ), (
            f"producer id {strategy_id!r} does not resolve to a known "
            "ServiceType — the realtime-strategies registry has drifted "
            "from the producer's get_enabled_strategies() list"
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
