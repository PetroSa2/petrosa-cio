"""Unit tests for FR53 / P3.4 stale-characterization refusal (petrosa-cio#130).

Covers the three layers shipped by this PR:

  1. ``is_characterization_stale`` — single HTTP GET against data-manager,
     with the documented 404 ⇒ stale, 200 ⇒ fresh, error ⇒ fail-open
     semantics.
  2. ``Orchestrator.run`` — when the gate reports stale, the loop short-
     circuits with `ActionType.REJECT` + `RejectionSource.STALE_CHARACTERIZATION`
     and never invokes the LLM personas.
  3. ``DecisionRecord`` / dashboard surface — the structured refusal source
     and the claimed revision id round-trip through the recent-decisions
     feed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cio.apps.nurse.models import RiskLimits
from cio.core.characterization_stale_gate import is_characterization_stale
from cio.core.decision_store import DecisionRecord, DecisionStore
from cio.models import (
    ConfidenceLevel,
    RegimeEnum,
    RegimeResult,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
    VolatilityLevel,
)
from cio.models.context import MarketSignals, PortfolioSummary
from cio.models.enums import ActionType, RegimeFit, RejectionSource, TriggerType

# ---------------------------------------------------------------------------
# Layer 1 — gate HTTP semantics
# ---------------------------------------------------------------------------


def _mock_async_client_returning(status_code: int) -> AsyncMock:
    """Build an httpx.AsyncClient stub whose `get` returns the given status."""
    response = MagicMock()
    response.status_code = status_code
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_gate_returns_false_when_revision_id_absent():
    """Legacy intent (no revision) is never refused."""
    assert (
        await is_characterization_stale(strategy_id="s1", strategy_revision_id=None)
        is False
    )
    assert (
        await is_characterization_stale(strategy_id="s1", strategy_revision_id="")
        is False
    )


@pytest.mark.asyncio
async def test_gate_returns_true_on_404():
    """404 from data-manager is the canonical 'stale → refuse' signal."""
    client = _mock_async_client_returning(404)
    stale = await is_characterization_stale(
        strategy_id="s1",
        strategy_revision_id="srev_abc",
        client=client,
    )
    assert stale is True
    client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_returns_false_on_200():
    client = _mock_async_client_returning(200)
    assert (
        await is_characterization_stale(
            strategy_id="s1", strategy_revision_id="srev_abc", client=client
        )
        is False
    )


@pytest.mark.asyncio
async def test_gate_fails_open_on_unexpected_status():
    """5xx / 4xx (other than 404) → log + fail-open (False)."""
    client = _mock_async_client_returning(503)
    assert (
        await is_characterization_stale(
            strategy_id="s1", strategy_revision_id="srev_abc", client=client
        )
        is False
    )


@pytest.mark.asyncio
async def test_gate_fails_open_on_request_error():
    """Connection / timeout → log + fail-open."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    assert (
        await is_characterization_stale(
            strategy_id="s1", strategy_revision_id="srev_abc", client=client
        )
        is False
    )


# ---------------------------------------------------------------------------
# Layer 2 — Orchestrator short-circuits with REJECT + STALE_CHARACTERIZATION
# ---------------------------------------------------------------------------


def _make_context(*, revision_id: str | None = "srev_abc") -> TriggerContext:
    return TriggerContext(
        correlation_id="cid-1",
        source_subject="cio.intent.trading.s1",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload={},
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.HIGH,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="t",
            confidence=0.9,
            fit=RegimeFit.GOOD,
            thought_trace="t",
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="ok",
            current_price=100.0,
            volatility_percentile=0.5,
            trend_strength=0.5,
            price_action_character="ranging",
        ),
        strategy_id="s1",
        strategy_revision_id=revision_id,
        strategy_stats=StrategyStats(),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_hours=4.0
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=1000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(),
    )


def _build_orchestrator():
    from cio.core.orchestrator import Orchestrator

    with patch("cio.core.orchestrator.ClientFactory.create", return_value=MagicMock()):
        return Orchestrator()


@pytest.mark.asyncio
async def test_orchestrator_rejects_when_characterization_is_stale():
    """Stale characterization short-circuits the reasoning loop."""
    orch = _build_orchestrator()
    context = _make_context(revision_id="srev_stale")

    with patch(
        "cio.core.orchestrator.is_characterization_stale",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_gate:
        result = await orch.run(context)

    mock_gate.assert_awaited_once()
    assert result.action == ActionType.REJECT
    assert result.rejection_source == RejectionSource.STALE_CHARACTERIZATION
    assert result.hard_blocked is True
    assert "stale_characterization" in (result.hard_block_reason or "")
    assert "srev_stale" in (result.hard_block_reason or "")


@pytest.mark.asyncio
async def test_orchestrator_skips_gate_when_no_revision_id():
    """Legacy intent (no revision id) skips the gate entirely."""
    orch = _build_orchestrator()
    context = _make_context(revision_id=None)

    with patch(
        "cio.core.orchestrator.is_characterization_stale",
        new_callable=AsyncMock,
        return_value=True,  # would refuse if called — but should NOT be called
    ) as mock_gate:
        # Force the deterministic-bypass path so the test does not depend on
        # the LLM personas: NURSE_USE_LLM_REASONING=false.
        orch.use_llm_reasoning = False
        result = await orch.run(context)

    mock_gate.assert_not_awaited()
    assert result.rejection_source is None


@pytest.mark.asyncio
async def test_orchestrator_passes_through_when_revision_is_fresh():
    """Fresh characterization (gate returns False) does not refuse."""
    orch = _build_orchestrator()
    context = _make_context(revision_id="srev_fresh")

    with patch(
        "cio.core.orchestrator.is_characterization_stale",
        new_callable=AsyncMock,
        return_value=False,
    ):
        orch.use_llm_reasoning = False  # take the deterministic-bypass branch
        result = await orch.run(context)

    assert result.rejection_source is None
    # The deterministic-bypass branch ultimately classifies the action; the
    # key invariant here is that the gate did NOT veto the loop.
    assert result.action != ActionType.REJECT or result.rejection_source is None


# ---------------------------------------------------------------------------
# Layer 3 — DecisionRecord + dashboard surface
# ---------------------------------------------------------------------------


def test_decision_record_carries_refusal_metadata():
    """DecisionRecord persists rejection_source + strategy_revision_id."""
    store = DecisionStore()
    store.record(
        DecisionRecord(
            strategy_id="s1",
            action="reject",
            reasoning_trace="STALE_CHARACTERIZATION",
            confidence=0.3,
            rejection_source="stale_characterization",
            strategy_revision_id="srev_stale",
        )
    )
    records = store.recent(datetime.now(UTC).replace(year=2020))
    assert len(records) == 1
    r = records[0]
    assert r.rejection_source == "stale_characterization"
    assert r.strategy_revision_id == "srev_stale"


def test_decision_record_defaults_refusal_metadata_to_none():
    """Existing call sites that do not set the new fields keep working (None default)."""
    record = DecisionRecord(
        strategy_id="s1",
        action="execute",
        reasoning_trace="ok",
        confidence=0.7,
    )
    assert record.rejection_source is None
    assert record.strategy_revision_id is None
