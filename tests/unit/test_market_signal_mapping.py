"""
Unit tests for #202: mapping real signal-producer fields onto MarketSignals,
explicit placeholder/gap recording, and the market_signals availability flag.

Covers:
  - AC1: a live-received signal payload populates MarketSignals from real
    values (confidence -> volatility_percentile, strength -> trend_strength,
    action -> price_action_character, metadata -> signal_summary).
  - AC2: a placeholder-only payload records degraded_fields + is_placeholder,
    emits a MARKET_SIGNALS_PLACEHOLDER_FIELDS WARNING, and appends
    ContextGap(surface='market_signals') to the emitted bundle, flipping
    market_signals_available to False.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.clients.llm_client import MockLLMClient
from cio.core.context_builder import ContextBuilder
from cio.core.orchestrator import Orchestrator
from cio.models import (
    SAFE_DECISION_RESULT,
    ActionType,
    ConfidenceLevel,
    PnlTrend,
    PortfolioSummary,
    RegimeEnum,
    RegimeResult,
    RiskLimits,
    StrategyDefaults,
    StrategyStats,
    TriggerType,
    VolatilityLevel,
)
from cio.personas.strategy_assessor import (
    REQUIRED_CONTEXT_FIELDS,
    StrategyAssessor,
)


def _make_builder() -> ContextBuilder:
    return ContextBuilder(
        data_manager_url="http://dm",
        tradeengine_url="http://te",
    )


def _regime() -> RegimeResult:
    return RegimeResult(
        regime=RegimeEnum.RANGING,
        regime_confidence=ConfidenceLevel.HIGH,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="test",
        thought_trace="test",
    )


def _risk_limits() -> RiskLimits:
    return RiskLimits(
        max_drawdown_pct=0.1,
        max_position_size_pct=0.5,
        volatility_scale_threshold=0.5,
        max_orders_global=50,
        max_orders_per_symbol=5,
        max_position_size_usd=1000.0,
    )


def _stub_fetches(builder: ContextBuilder) -> None:
    builder._fetch_regime = AsyncMock(return_value=_regime())
    builder._fetch_portfolio_and_risk = AsyncMock(
        return_value=(
            PortfolioSummary(
                gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
            ),
            _risk_limits(),
            {},
        )
    )
    builder._fetch_strategy_data = AsyncMock(
        return_value=(
            StrategyStats(),
            StrategyDefaults(
                stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
            ),
        )
    )


# ---------------------------------------------------------------------------
# AC1: real producer fields map onto MarketSignals
# ---------------------------------------------------------------------------


def test_build_market_signals_maps_real_producer_fields():
    builder = _make_builder()
    gaps = []
    payload = {
        "symbol": "BTCUSDT",
        "confidence": 0.82,
        "strength": "strong",
        "action": "buy",
        "current_price": 65000.0,
        "metadata": {"signal_summary": "EMA alignment bullish"},
    }

    market_signals = builder._build_market_signals(payload, "cid-real", gaps=gaps)

    assert market_signals.signal_summary == "EMA alignment bullish"
    assert market_signals.volatility_percentile == 0.82
    assert market_signals.trend_strength == 0.75
    assert market_signals.price_action_character == "Bullish"
    assert market_signals.current_price == 65000.0
    assert market_signals.degraded_fields == []
    assert market_signals.is_placeholder is False
    assert gaps == []


def test_build_market_signals_explicit_fields_win_over_derivation():
    builder = _make_builder()
    payload = {
        "volatility_percentile": 0.2,
        "trend_strength": 0.1,
        "price_action_character": "Choppy",
        "signal_summary": "Explicit",
        "confidence": 0.9,
        "strength": "extreme",
        "action": "buy",
    }

    market_signals = builder._build_market_signals(payload, "cid-explicit")

    assert market_signals.signal_summary == "Explicit"
    assert market_signals.volatility_percentile == 0.2
    assert market_signals.trend_strength == 0.1
    assert market_signals.price_action_character == "Choppy"
    assert market_signals.is_placeholder is False


def test_build_market_signals_derives_summary_from_metadata_reasoning():
    """Live RTS/iceberg payloads carry `metadata.reasoning` (not an explicit
    signal_summary), so the profile must derive the summary from it instead of
    degrading signal_summary on every real signal."""
    builder = _make_builder()
    gaps = []
    payload = {
        "symbol": "BTCUSDT",
        "confidence": 0.75,
        "strength": "strong",
        "action": "sell",
        "current_price": 76551.55,
        "metadata": {
            "reasoning": "Large hidden seller detected at 76546.1 (anchor)",
        },
    }

    market_signals = builder._build_market_signals(payload, "cid-reasoning", gaps=gaps)

    assert market_signals.signal_summary == (
        "Large hidden seller detected at 76546.1 (anchor)"
    )
    assert market_signals.volatility_percentile == 0.75
    assert market_signals.trend_strength == 0.75
    assert market_signals.price_action_character == "Bearish"
    assert market_signals.degraded_fields == []
    assert market_signals.is_placeholder is False
    assert gaps == []


# ---------------------------------------------------------------------------
# AC2: placeholder fields are recorded, never silent
# ---------------------------------------------------------------------------


def test_build_market_signals_placeholder_payload_records_gap_and_warning(caplog):
    caplog.set_level(logging.INFO, logger="cio.core.context_builder")
    builder = _make_builder()
    gaps = []

    market_signals = builder._build_market_signals(
        {"symbol": "BTCUSDT"}, "cid-placeholder", gaps=gaps
    )

    assert market_signals.is_placeholder is True
    assert market_signals.degraded_fields == [
        "price_action_character",
        "signal_summary",
        "trend_strength",
        "volatility_percentile",
    ]
    assert market_signals.signal_summary == "Manual trigger"
    assert market_signals.volatility_percentile == 0.5
    assert market_signals.trend_strength == 0.0
    assert market_signals.price_action_character == "Neutral"

    assert len(gaps) == 1
    assert gaps[0].surface == "market_signals"
    assert gaps[0].reason.startswith("placeholder_fields: ")

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "MARKET_SIGNALS_PLACEHOLDER_FIELDS" in r.message
    ]
    assert warnings
    assert "signal_summary" in warnings[0].message


def test_build_market_signals_partial_degradation_keeps_real_fields():
    builder = _make_builder()
    gaps = []

    market_signals = builder._build_market_signals(
        {"confidence": 0.4, "current_price": 100.0}, "cid-partial", gaps=gaps
    )

    assert market_signals.volatility_percentile == 0.4
    assert market_signals.current_price == 100.0
    assert market_signals.degraded_fields == [
        "price_action_character",
        "signal_summary",
        "trend_strength",
    ]
    assert market_signals.is_placeholder is True
    assert len(gaps) == 1
    assert gaps[0].surface == "market_signals"


def test_build_market_signals_without_gaps_collector_keeps_legacy_contract():
    builder = _make_builder()

    market_signals = builder._build_market_signals({"symbol": "BTCUSDT"}, "cid-nogaps")

    assert market_signals.is_placeholder is True


# ---------------------------------------------------------------------------
# AC2: the emitted bundle carries the availability flag + gap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_flags_placeholder_market_signals_unavailable():
    builder = _make_builder()
    _stub_fetches(builder)

    ctx = await builder.build(
        correlation_id="cid-wire-placeholder",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={"symbol": "BTCUSDT", "strategy_id": "s1"},
    )

    assert ctx.market_signals.is_placeholder is True
    assert ctx.pre_decision_context is not None
    assert ctx.pre_decision_context.market_signals_available is False
    assert any(g.surface == "market_signals" for g in ctx.pre_decision_context.gaps)

    await builder.close()


@pytest.mark.asyncio
async def test_build_marks_real_market_signals_available_without_gap():
    builder = _make_builder()
    _stub_fetches(builder)

    ctx = await builder.build(
        correlation_id="cid-wire-real",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={
            "symbol": "BTCUSDT",
            "strategy_id": "s1",
            "confidence": 0.7,
            "strength": "medium",
            "action": "sell",
            "current_price": 50000.0,
            "metadata": {"summary": "iceberg persistence"},
        },
    )

    assert ctx.market_signals.is_placeholder is False
    assert ctx.market_signals.price_action_character == "Bearish"
    assert ctx.pre_decision_context is not None
    assert ctx.pre_decision_context.market_signals_available is True
    assert not any(g.surface == "market_signals" for g in ctx.pre_decision_context.gaps)

    await builder.close()


# ---------------------------------------------------------------------------
# AC4: a decision cycle with complete context does not collapse to pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_context_decision_cycle_does_not_pause(monkeypatch):
    """AC4 — with real signal values flowing into MarketSignals (AC1) the
    reasoning loop produces a non-pause verdict for a healthy buy signal.
    The CodeEngine is stubbed clean so the assertion isolates the persona
    path (regime -> strategy -> action) that MISSING_INPUT used to force
    onto SAFE_DEFAULTS.
    """
    monkeypatch.setenv("NURSE_USE_LLM_REASONING", "true")

    builder = _make_builder()
    _stub_fetches(builder)
    context = await builder.build(
        correlation_id="cid-ac4",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={
            "symbol": "BTCUSDT",
            "strategy_id": "s1",
            "confidence": 0.85,
            "strength": "extreme",
            "action": "buy",
            "current_price": 65000.0,
            "metadata": {"signal_summary": "EMA alignment bullish"},
        },
    )
    assert context.market_signals.is_placeholder is False

    with patch("cio.core.orchestrator.CodeEngine") as mock_engine:
        code_result = MagicMock()
        code_result.hard_blocked = False
        code_result.block_reason = None
        code_result.gross_ev = 1.0
        code_result.ev_unavailable = False
        code_result.kelly_position_usd = 0.0
        code_result.risk_warnings = []
        mock_engine.run.return_value = code_result

        orchestrator = Orchestrator(llm_client=MockLLMClient())
        assert orchestrator.use_llm_reasoning is True

        decision = await orchestrator.run(context)

    assert decision.action != ActionType.PAUSE_STRATEGY
    assert decision.action == ActionType.EXECUTE
    assert decision != SAFE_DECISION_RESULT

    await builder.close()


# ---------------------------------------------------------------------------
# #202 follow-up: Strategy Assessor names the missing required inputs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_assessor_warns_and_gaps_on_missing_stats(caplog):
    """When strategy_stats is empty (upstream unavailable) the assessor must
    name the absent required fields and record a strategy_stats ContextGap
    rather than leaving only the LLM's generic MISSING_INPUT self-report."""
    caplog.set_level(logging.WARNING, logger="cio.personas.strategy_assessor")
    builder = _make_builder()
    _stub_fetches(builder)  # StrategyStats() -> all performance fields None
    context = await builder.build(
        correlation_id="cid-assessor-missing",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={"symbol": "BTCUSDT", "strategy_id": "s1"},
    )

    assessor = StrategyAssessor(MockLLMClient())
    user_context = assessor._build_user_context(context)
    assessor._warn_on_missing_required_fields(context, user_context)

    assert user_context["win_rate"] is None
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "STRATEGY_ASSESSOR_MISSING_INPUT_FIELDS" in r.message
    ]
    assert warnings
    assert "win_rate" in warnings[0].message
    assert "recent_pnl_trend" in warnings[0].message
    assert "regime" not in warnings[0].message

    assert context.pre_decision_context is not None
    stats_gaps = [
        g for g in context.pre_decision_context.gaps if g.surface == "strategy_stats"
    ]
    assert stats_gaps
    assert "win_rate" in stats_gaps[-1].reason

    await builder.close()


@pytest.mark.asyncio
async def test_strategy_assessor_silent_when_context_complete(caplog):
    caplog.set_level(logging.WARNING, logger="cio.personas.strategy_assessor")
    builder = _make_builder()
    _stub_fetches(builder)
    builder._fetch_strategy_data = AsyncMock(
        return_value=(
            StrategyStats(
                win_rate=0.6,
                win_rate_delta=0.05,
                consecutive_losses=0,
                recent_pnl_trend=PnlTrend.NEUTRAL,
            ),
            StrategyDefaults(
                stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
            ),
        )
    )
    context = await builder.build(
        correlation_id="cid-assessor-complete",
        source_subject="intent.test",
        trigger_type=TriggerType.TRADE_INTENT,
        payload={"symbol": "BTCUSDT", "strategy_id": "s1"},
    )

    assessor = StrategyAssessor(MockLLMClient())
    user_context = assessor._build_user_context(context)
    assert set(REQUIRED_CONTEXT_FIELDS) <= set(user_context)
    assessor._warn_on_missing_required_fields(context, user_context)

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "STRATEGY_ASSESSOR_MISSING_INPUT_FIELDS" in r.message
    ]
    assert not warnings
    assert context.pre_decision_context is not None
    assert not any(
        g.surface == "strategy_stats" for g in context.pre_decision_context.gaps
    )

    await builder.close()
