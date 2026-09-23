"""Unit tests for #234 AC1 — concurrent regime/strategy persona resolution.

`Orchestrator.run`'s regime_analyst.classify and strategy_assessor.assess
calls are independent (both take only `context`) but were previously run
sequentially under NurseEnforcer's single umbrella audit timeout. They now
resolve concurrently via `asyncio.gather`, with each resolver short-
circuiting to a cached value with no I/O when available. These tests cover
the three cache-state permutations that matter:

  1. COLD/COLD  — neither cached: both personas invoked, both results cached.
  2. HOT/HOT    — both cached: neither persona invoked at all.
  3. HOT/COLD   — regime cached, strategy not: only strategy_assessor called.

This also closes the patch-coverage gap flagged on PR #235 for the
cache-hit branches in `Orchestrator.run`, which were untested before this
refactor touched those exact lines.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.orchestrator import Orchestrator
from cio.models import (
    ConfidenceLevel,
    MarketSignals,
    PnlTrend,
    PortfolioSummary,
    RegimeEnum,
    RegimeFit,
    RegimeResult,
    RiskLimits,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)


def _make_context() -> TriggerContext:
    return TriggerContext(
        correlation_id="test-persona-concurrency",
        trigger_type=TriggerType.TRADE_INTENT,
        strategy_id="test_strat",
        symbol="BTCUSDT",
        source_subject="cio.intent.trading.test_strat",
        trigger_payload={"symbol": "BTCUSDT"},
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.HIGH,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="test",
            confidence=1.0,
            fit=RegimeFit.GOOD,
            thought_trace="test",
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="bullish",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.7,
            price_action_character="stable",
        ),
        strategy_stats=StrategyStats(recent_pnl_trend=PnlTrend.NEUTRAL),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=1000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(
            max_drawdown_pct=0.1,
            max_orders_global=50,
            max_orders_per_symbol=5,
            max_position_size_usd=1000.0,
        ),
    )


def _make_regime_result() -> RegimeResult:
    return RegimeResult(
        regime=RegimeEnum.RANGING,
        regime_confidence=ConfidenceLevel.HIGH,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="cached",
        confidence=1.0,
        fit=RegimeFit.GOOD,
        thought_trace="cached",
    )


def _make_strategy_result():
    from cio.models import ActivationRecommendation, HealthStatus, StrategyResult

    return StrategyResult(
        strategy_id="test_strat",
        health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        regime_fit=RegimeFit.GOOD,
        confidence=1.0,
        thought_trace="cached",
    )


def _mock_cache(regime_json: str | None, strategy_json: str | None) -> MagicMock:
    cache = MagicMock()

    async def _get(key: str):
        if key.startswith("regime:"):
            return regime_json
        if key.startswith("strategy:"):
            return strategy_json
        return None

    cache.get = AsyncMock(side_effect=_get)
    cache.set = AsyncMock()
    # #199: the context-completeness gate calls cache.delete() on every
    # healthy cycle to clear a stale streak counter — must be awaitable.
    cache.delete = AsyncMock()
    return cache


def _patched_engine_no_block():
    """Patch target for a CodeEngine that never hard-blocks and sizes 0."""
    mock_code_result = MagicMock()
    mock_code_result.hard_blocked = False
    mock_code_result.kelly_position_usd = 0.0
    return mock_code_result


@pytest.mark.asyncio
async def test_cold_cold_invokes_both_personas_and_caches_both():
    """Neither surface cached: both personas are invoked (concurrently) and cached."""
    with (
        patch("cio.core.orchestrator.CodeEngine") as MockEngine,
        patch("cio.core.orchestrator.RegimeAnalyst") as MockRegime,
        patch("cio.core.orchestrator.StrategyAssessor") as MockStrategy,
        patch("cio.core.orchestrator.ActionClassifier") as MockClassifier,
    ):
        MockEngine.run.return_value = _patched_engine_no_block()

        regime_result = _make_regime_result()
        strategy_result = _make_strategy_result()
        MockRegime.return_value.classify = AsyncMock(return_value=regime_result)
        MockStrategy.return_value.assess = AsyncMock(return_value=strategy_result)
        MockClassifier.return_value.classify = AsyncMock(return_value=MagicMock())

        cache = _mock_cache(regime_json=None, strategy_json=None)
        orchestrator = Orchestrator(cache=cache)
        context = _make_context()

        await orchestrator.run(context)

        MockRegime.return_value.classify.assert_awaited_once_with(context)
        MockStrategy.return_value.assess.assert_awaited_once_with(context)

        cached_keys = {c.args[0] for c in cache.set.call_args_list}
        assert f"regime:{context.strategy_id}" in cached_keys
        assert f"strategy:{context.strategy_id}" in cached_keys

        MockClassifier.return_value.classify.assert_awaited_once()
        args, _ = MockClassifier.return_value.classify.call_args
        assert args[2] is regime_result
        assert args[3] is strategy_result


@pytest.mark.asyncio
async def test_hot_hot_skips_both_personas():
    """Both surfaces cached: neither persona is invoked at all."""
    with (
        patch("cio.core.orchestrator.CodeEngine") as MockEngine,
        patch("cio.core.orchestrator.RegimeAnalyst") as MockRegime,
        patch("cio.core.orchestrator.StrategyAssessor") as MockStrategy,
        patch("cio.core.orchestrator.ActionClassifier") as MockClassifier,
    ):
        MockEngine.run.return_value = _patched_engine_no_block()
        MockClassifier.return_value.classify = AsyncMock(return_value=MagicMock())

        regime_result = _make_regime_result()
        strategy_result = _make_strategy_result()
        cache = _mock_cache(
            regime_json=regime_result.model_dump_json(),
            strategy_json=strategy_result.model_dump_json(),
        )
        orchestrator = Orchestrator(cache=cache)
        context = _make_context()

        await orchestrator.run(context)

        MockRegime.return_value.classify.assert_not_called()
        MockStrategy.return_value.assess.assert_not_called()
        # No fresh values to cache — cache.set should not be called for
        # either surface on a full cache hit.
        cache.set.assert_not_called()

        MockClassifier.return_value.classify.assert_awaited_once()
        args, _ = MockClassifier.return_value.classify.call_args
        assert args[2] == regime_result
        assert args[3] == strategy_result


@pytest.mark.asyncio
async def test_hot_cold_mix_only_invokes_missing_persona():
    """Regime cached, strategy not: only strategy_assessor.assess is invoked."""
    with (
        patch("cio.core.orchestrator.CodeEngine") as MockEngine,
        patch("cio.core.orchestrator.RegimeAnalyst") as MockRegime,
        patch("cio.core.orchestrator.StrategyAssessor") as MockStrategy,
        patch("cio.core.orchestrator.ActionClassifier") as MockClassifier,
    ):
        MockEngine.run.return_value = _patched_engine_no_block()
        strategy_result = _make_strategy_result()
        MockStrategy.return_value.assess = AsyncMock(return_value=strategy_result)
        MockClassifier.return_value.classify = AsyncMock(return_value=MagicMock())

        regime_result = _make_regime_result()
        cache = _mock_cache(
            regime_json=regime_result.model_dump_json(), strategy_json=None
        )
        orchestrator = Orchestrator(cache=cache)
        context = _make_context()

        await orchestrator.run(context)

        MockRegime.return_value.classify.assert_not_called()
        MockStrategy.return_value.assess.assert_awaited_once_with(context)

        cached_keys = {c.args[0] for c in cache.set.call_args_list}
        assert cached_keys == {f"strategy:{context.strategy_id}"}
