import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from test_engine import build_test_context

from cio.core.context_builder import ContextBuilder
from cio.core.engine import CodeEngine
from cio.models import (
    SAFE_DEFAULTS,
    ContextGap,
    PnlTrend,
    StrategyStats,
)
from cio.models.enums import (
    ActivationRecommendation,
    HealthStatus,
    RegimeEnum,
    RegimeFit,
)
from cio.personas.strategy_assessor import COLD_START_TRACE, StrategyAssessor


def _make_builder() -> ContextBuilder:
    return ContextBuilder(data_manager_url="http://dm", tradeengine_url="http://te")


def _response(stats: dict, metadata: dict | None = None) -> MagicMock:
    payload = {"stats": stats}
    if metadata is not None:
        payload["metadata"] = metadata
    return MagicMock(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: payload,
    )


def _null_stats(**overrides) -> dict:
    stats = {
        "win_rate": None,
        "win_rate_delta": None,
        "consecutive_losses": None,
        "recent_pnl_trend": "neutral",
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
    }
    stats.update(overrides)
    return stats


async def _fetch(builder: ContextBuilder, stats: dict, metadata: dict | None = None):
    builder.client.get = AsyncMock(return_value=_response(stats, metadata))
    gaps: list[ContextGap] = []
    result = await builder._fetch_strategy_stats("strategy-1", "cid", gaps=gaps)
    await builder.close()
    return result, gaps


@pytest.mark.asyncio
async def test_fetch_stats_zero_fills_is_insufficient_history(caplog):
    caplog.set_level(logging.INFO, logger="cio.core.context_builder")
    result, gaps = await _fetch(
        _make_builder(),
        _null_stats(),
        {"source": "data-manager-pnl-calculator", "fills_replayed": 0},
    )
    assert result.history_status == "insufficient_history"
    assert not [gap for gap in gaps if gap.surface == "strategy_stats"]
    assert not any(
        "STRATEGY_STATS_STRUCTURAL_GAP" in record.message for record in caplog.records
    )
    assert any(
        "STRATEGY_STATS_INSUFFICIENT_HISTORY" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_fetch_stats_fills_without_closed_trades_is_insufficient_history():
    result, gaps = await _fetch(
        _make_builder(),
        _null_stats(),
        {"source": "data-manager-pnl-calculator", "fills_replayed": 225},
    )
    assert result.history_status == "insufficient_history"
    assert not [gap for gap in gaps if gap.surface == "strategy_stats"]


@pytest.mark.asyncio
async def test_fetch_stats_single_closed_trade_is_insufficient_history():
    result, _ = await _fetch(
        _make_builder(),
        _null_stats(win_rate=1.0, consecutive_losses=0),
        {"source": "data-manager-pnl-calculator", "fills_replayed": 1},
    )
    assert result.history_status == "insufficient_history"
    assert result.win_rate == 1.0


@pytest.mark.asyncio
async def test_fetch_stats_complete_is_computed():
    result, gaps = await _fetch(
        _make_builder(),
        _null_stats(win_rate=0.5, win_rate_delta=0.1, consecutive_losses=1),
    )
    assert result.history_status == "computed"
    assert not [gap for gap in gaps if gap.surface == "strategy_stats"]


@pytest.mark.asyncio
async def test_fetch_stats_no_db_source_is_unavailable(caplog):
    caplog.set_level(logging.WARNING, logger="cio.core.context_builder")
    result, gaps = await _fetch(
        _make_builder(),
        _null_stats(),
        {"source": "data-manager-analysis-no-db", "fills_replayed": 0},
    )
    assert result.history_status == "unavailable"
    assert any(gap.reason.startswith("structural_gap:") for gap in gaps)
    assert any(
        "STRATEGY_STATS_STRUCTURAL_GAP" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_fetch_stats_missing_key_is_unavailable():
    stats = _null_stats()
    del stats["win_rate"]
    result, _ = await _fetch(
        _make_builder(),
        stats,
        {"source": "data-manager-pnl-calculator", "fills_replayed": 0},
    )
    assert result.history_status == "unavailable"


@pytest.mark.asyncio
async def test_fetch_stats_missing_metadata_is_unavailable():
    result, _ = await _fetch(_make_builder(), _null_stats())
    assert result.history_status == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadTimeout(""), RuntimeError("down")])
async def test_fetch_stats_timeout_and_error_are_unavailable(failure):
    builder = _make_builder()
    builder.client.get = AsyncMock(side_effect=failure)
    result = await builder._fetch_strategy_stats("strategy-1", "cid", gaps=[])
    await builder.close()
    assert result.history_status == "unavailable"


@pytest.mark.asyncio
async def test_fetch_stats_ignores_upstream_history_status():
    stats = _null_stats(history_status="computed", fill_count=5)
    result, _ = await _fetch(
        _make_builder(),
        stats,
        {"source": "data-manager-pnl-calculator", "fills_replayed": 5},
    )
    assert result.history_status == "insufficient_history"


def _assessor_context(history_status: str | None):
    context = build_test_context(win_rate=0.0, portfolio_state_available=True)
    context.strategy_stats = StrategyStats(
        win_rate=None,
        win_rate_delta=None,
        consecutive_losses=None,
        recent_pnl_trend=PnlTrend.NEUTRAL,
        history_status=history_status,
    )
    return context


@pytest.mark.asyncio
async def test_assessor_cold_start_skips_llm(caplog):
    client = MagicMock()
    client.capability_profile = "standard"
    client.complete_with_schema = AsyncMock()
    assessor = StrategyAssessor(client)
    context = _assessor_context("insufficient_history")
    caplog.set_level(logging.INFO, logger="cio.personas.strategy_assessor")

    result = await assessor.assess(context)

    assert client.complete_with_schema.await_count == 0
    assert result.health == HealthStatus.HEALTHY
    assert result.regime_fit == RegimeFit.NEUTRAL
    assert result.activation_recommendation == ActivationRecommendation.RUN
    assert result.thought_trace == COLD_START_TRACE
    assert result is not SAFE_DEFAULTS["PETROSA_PROMPT_STRATEGY_ASSESSOR"]
    assert not any(
        "STRATEGY_ASSESSOR_MISSING_INPUT_FIELDS" in record.message
        for record in caplog.records
    )
    assert context.pre_decision_context is not None
    assert not [
        gap
        for gap in context.pre_decision_context.gaps
        if gap.surface == "strategy_stats"
    ]


@pytest.mark.asyncio
async def test_assessor_cold_start_disabled_by_env(monkeypatch, caplog):
    monkeypatch.setenv("CIO_COLD_START_ENABLED", "false")
    client = MagicMock()
    client.capability_profile = "standard"
    client.complete_with_schema = AsyncMock(
        return_value=SAFE_DEFAULTS["PETROSA_PROMPT_STRATEGY_ASSESSOR"]
    )
    assessor = StrategyAssessor(client)
    context = _assessor_context("insufficient_history")
    caplog.set_level(logging.WARNING, logger="cio.personas.strategy_assessor")

    await assessor.assess(context)

    assert client.complete_with_schema.await_count == 1
    assert any(
        "STRATEGY_ASSESSOR_MISSING_INPUT_FIELDS" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("history_status", ["unavailable", None])
async def test_assessor_unavailable_still_uses_missing_input_path(
    history_status, caplog
):
    client = MagicMock()
    client.capability_profile = "standard"
    client.complete_with_schema = AsyncMock(
        return_value=SAFE_DEFAULTS["PETROSA_PROMPT_STRATEGY_ASSESSOR"]
    )
    assessor = StrategyAssessor(client)
    context = _assessor_context(history_status)
    caplog.set_level(logging.WARNING, logger="cio.personas.strategy_assessor")

    await assessor.assess(context)

    assert client.complete_with_schema.await_count == 1
    assert any(
        "STRATEGY_ASSESSOR_MISSING_INPUT_FIELDS" in record.message
        for record in caplog.records
    )
    assert context.pre_decision_context is not None
    assert any(
        gap.surface == "strategy_stats" for gap in context.pre_decision_context.gaps
    )


def test_engine_insufficient_history_ignores_win_rate():
    context = build_test_context(win_rate=1.0)
    context.strategy_stats.history_status = "insufficient_history"
    result = CodeEngine.run(context)
    assert result.ev_unavailable is True
    assert result.kelly_position_usd is None
    assert result.gross_ev is None


def test_engine_computed_history_unchanged():
    legacy = build_test_context(win_rate=0.6)
    computed = build_test_context(win_rate=0.6)
    computed.strategy_stats.history_status = "computed"
    legacy_result = CodeEngine.run(legacy)
    computed_result = CodeEngine.run(computed)
    assert computed_result.gross_ev == legacy_result.gross_ev
    assert computed_result.kelly_position_usd == legacy_result.kelly_position_usd


def test_cold_start_regime_hard_block_still_blocks():
    context = build_test_context(win_rate=0.0)
    context.strategy_stats.history_status = "insufficient_history"
    context.regime.regime = RegimeEnum.CHOPPY
    result = CodeEngine.run(context)
    assert result.hard_blocked is True
    assert result.block_reason is not None
    assert result.block_reason.startswith("regime_block: CHOPPY")
