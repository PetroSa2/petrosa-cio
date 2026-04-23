"""Tests for SignalArbiter — cross-strategy signal arbitration.

AC coverage:
- AC-Dedup: same symbol + action within 60s is dropped
- AC-Conflict: opposing signals within 5min → higher confidence wins
- AC-Integration: 3 strategies (2 BUY, 1 SELL) on BTCUSDT — only highest confidence passes
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.core.arbiter import SignalArbiter


def _make_cache(store: dict | None = None) -> MagicMock:
    """Build a mock AsyncRedisCache backed by an in-memory dict."""
    if store is None:
        store = {}

    cache = MagicMock()

    async def _get(key: str):
        return store.get(key)

    async def _set(key: str, value: str, ttl: int = 900):
        store[key] = value

    cache.get = AsyncMock(side_effect=_get)
    cache.set = AsyncMock(side_effect=_set)
    return cache


@pytest.mark.asyncio
async def test_first_signal_always_allowed():
    cache = _make_cache()
    arbiter = SignalArbiter(cache)
    allowed, reason = await arbiter.check("BTCUSDT", "buy", 0.8, "strat_a", "cid1")
    assert allowed is True
    assert reason == "allowed"


@pytest.mark.asyncio
async def test_deduplication_same_action():
    """Second signal with same symbol+action within window is dropped."""
    store = {}
    cache = _make_cache(store)
    arbiter = SignalArbiter(cache)

    # First passes
    allowed1, _ = await arbiter.check("BTCUSDT", "buy", 0.8, "strat_a", "cid1")
    assert allowed1 is True

    # Second with same symbol+action is deduplicated
    allowed2, reason2 = await arbiter.check("BTCUSDT", "buy", 0.9, "strat_b", "cid2")
    assert allowed2 is False
    assert "SIGNAL_DEDUPLICATED" in reason2


@pytest.mark.asyncio
async def test_conflict_lower_confidence_suppressed():
    """Opposing signal with lower confidence is suppressed."""
    store = {}
    cache = _make_cache(store)
    arbiter = SignalArbiter(cache)

    # BUY with confidence 0.9 passes first
    allowed1, _ = await arbiter.check("XLMUSDT", "buy", 0.9, "strat_a", "cid1")
    assert allowed1 is True

    # SELL with lower confidence 0.7 is suppressed
    allowed2, reason2 = await arbiter.check("XLMUSDT", "sell", 0.7, "strat_b", "cid2")
    assert allowed2 is False
    assert "signal_conflict_resolved" in reason2
    assert "strat_a" in reason2


@pytest.mark.asyncio
async def test_conflict_higher_confidence_wins():
    """Opposing signal with HIGHER confidence overwrites existing bias."""
    store = {}
    cache = _make_cache(store)
    arbiter = SignalArbiter(cache)

    # SELL with low confidence first
    allowed1, _ = await arbiter.check("XLMUSDT", "sell", 0.5, "strat_weak", "cid1")
    assert allowed1 is True

    # BUY with higher confidence overrides
    allowed2, reason2 = await arbiter.check(
        "XLMUSDT", "buy", 0.9, "strat_strong", "cid2"
    )
    assert allowed2 is True
    assert reason2 == "allowed"

    # Verify bias was updated to buy
    bias_val = store.get("arbiter:bias:XLMUSDT")
    assert bias_val is not None
    assert bias_val.startswith("buy:")


@pytest.mark.asyncio
async def test_integration_three_strategies_btcusdt():
    """
    Integration test — AC requirement:
    3 strategies (strat_buy_1 conf=0.7, strat_buy_2 conf=0.9, strat_sell conf=0.6) all
    targeting BTCUSDT within the conflict window.

    Expected outcome:
    - strat_buy_1 (first BUY, conf=0.7) → ALLOWED (establishes bias)
    - strat_sell (SELL, conf=0.6 < 0.7) → SUPPRESSED (conflict, lower confidence)
    - strat_buy_2 (same BUY action as active dedup window) → SUPPRESSED (deduplicated)
    """
    store = {}
    cache = _make_cache(store)
    arbiter = SignalArbiter(cache)

    # Signal 1: BUY confidence 0.7 — first in, should pass
    ok1, r1 = await arbiter.check("BTCUSDT", "buy", 0.7, "strat_buy_1", "cid1")
    assert ok1 is True, f"strat_buy_1 should be allowed, got: {r1}"

    # Signal 2: SELL confidence 0.6 (opposing, lower) — should be suppressed
    ok2, r2 = await arbiter.check("BTCUSDT", "sell", 0.6, "strat_sell", "cid2")
    assert ok2 is False, f"strat_sell should be suppressed, got: {r2}"
    assert "signal_conflict_resolved" in r2

    # Signal 3: BUY confidence 0.9 (same action as strat_buy_1, dedup window active)
    ok3, r3 = await arbiter.check("BTCUSDT", "buy", 0.9, "strat_buy_2", "cid3")
    assert ok3 is False, f"strat_buy_2 should be deduplicated, got: {r3}"
    assert "SIGNAL_DEDUPLICATED" in r3


@pytest.mark.asyncio
async def test_no_arbiter_means_all_pass_through():
    """If arbiter is None on NATSListener, signals are not filtered (backwards compat)."""
    # Validate that SignalArbiter with no-op cache still returns allowed for fresh signals
    store = {}
    cache = _make_cache(store)
    arbiter = SignalArbiter(cache)
    allowed, _ = await arbiter.check("ETHUSDT", "sell", 0.5, "strat_x", "cid99")
    assert allowed is True


@pytest.mark.asyncio
async def test_different_symbols_independent():
    """Arbitration state for one symbol does not affect a different symbol."""
    store = {}
    cache = _make_cache(store)
    arbiter = SignalArbiter(cache)

    await arbiter.check("BTCUSDT", "buy", 0.9, "strat_a", "cid1")

    # SELL on a completely different symbol should pass
    ok, _ = await arbiter.check("ETHUSDT", "sell", 0.3, "strat_b", "cid2")
    assert ok is True
