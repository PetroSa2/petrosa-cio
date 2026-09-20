"""P1.4-AC4 (#132) — OutputRouter captures the PreDecisionContext bundle
(including any ContextGap entries) on the dashboard DecisionRecord.

petrosa-cio#215: this file used to also assert that the router published one
``cio.context.gap.<surface>`` NATS message per gap so a data-manager FR12
audit-trail consumer could persist them. That consumer was never built —
data-manager's subscriber inventory has no ``cio.context.gap.*`` entry — so
the publish was dead code with zero subscribers and has been removed from
``OutputRouter.route`` (see the comment there for the full rationale). The
gap data itself is not lost: it is captured below via
``DecisionStore.record(...)``, which is what ``/api/dashboard/decisions/recent``
actually reads.
"""

from __future__ import annotations

import os
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.decision_store import DecisionStore
from cio.core.router import OutputRouter
from cio.models import (
    ActionType,
    ActivationRecommendation,
    CharacterizationRef,
    ConfidenceLevel,
    ContextGap,
    DecisionResult,
    EvaluatorVerdict,
    HealthStatus,
    MarketState,
    PortfolioState,
    PreDecisionContext,
    RegimeFit,
    TriggerContext,
)
from cio.models.enums import RegimeEnum, VolatilityLevel


def _make_decision(action: ActionType) -> DecisionResult:
    return DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=action,
        justification="gap-publish test",
        thought_trace="trace",
    )


def _make_bundle(*, with_gaps: bool) -> PreDecisionContext:
    return PreDecisionContext(
        market_state=MarketState(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            current_price=10000.0,
            primary_signal="ok",
        ),
        portfolio_state=PortfolioState(
            gross_exposure=0.1,
            same_asset_pct=0.05,
            open_positions_count=1,
            global_drawdown_pct=0.0,
            available_capital_usd=5000.0,
            open_orders_global=0,
            open_orders_symbol=0,
        ),
        evaluator_verdicts={
            "ingest": EvaluatorVerdict(subsystem="ingest", verdict="healthy", reason="")
        },
        characterization=CharacterizationRef(
            strategy_id="strat-gap",
            strategy_revision_id="srev_aaaaaaaaaaaa_bbbbbbbbbbbb",
        ),
        evaluator_verdicts_available=not with_gaps,
        characterization_available=True,
        gaps=(
            [
                ContextGap(surface="evaluators", reason="subscriber_not_wired"),
                ContextGap(surface="characterization", reason="endpoint_500"),
            ]
            if with_gaps
            else []
        ),
    )


def _make_context(*, bundle: PreDecisionContext | None) -> TriggerContext:
    ctx = MagicMock(spec=TriggerContext)
    ctx.strategy_id = "strat-gap"
    ctx.decision_id = "decision-gap-132"
    ctx.correlation_id = "corr-gap-132"
    ctx.trigger_payload = {"symbol": "BTCUSDT"}
    ctx.strategy_revision_id = "srev_aaaaaaaaaaaa_bbbbbbbbbbbb"
    ctx.pre_decision_context = bundle
    return ctx


@pytest.mark.asyncio
async def test_router_stores_bundle_on_decision_record():
    """AC4.a — DecisionStore.record(...) captures the bundle so
    /api/dashboard/decisions/recent can return it, independent of any NATS
    publish."""
    bundle = _make_bundle(with_gaps=True)
    ctx = _make_context(bundle=bundle)
    store = DecisionStore()
    router = OutputRouter(
        nats_client=AsyncMock(),
        vector_client=AsyncMock(),
        ta_bot_url="http://ta-bot",
        decision_store=store,
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(ctx, _make_decision(ActionType.ADMIT))

    from datetime import datetime, timedelta

    recent = store.recent(datetime.now(UTC) - timedelta(minutes=1))
    assert len(recent) == 1
    rec = recent[0]
    assert rec.pre_decision_context is not None
    assert rec.pre_decision_context.evaluator_verdicts_available is False
    assert len(rec.pre_decision_context.gaps) == 2
    assert rec.decision_id == "decision-gap-132"


@pytest.mark.asyncio
async def test_router_no_longer_publishes_context_gap_events():
    """Regression guard for petrosa-cio#215: the dead
    ``cio.context.gap.<surface>`` publish must stay removed. If a future
    change resurrects it, wire a real consumer instead and update this
    test rather than reverting it."""
    bundle = _make_bundle(with_gaps=True)
    ctx = _make_context(bundle=bundle)
    mock_nc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=AsyncMock(),
        ta_bot_url="http://ta-bot",
        decision_store=DecisionStore(),
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(ctx, _make_decision(ActionType.ADMIT))

    gap_calls = [
        c
        for c in mock_nc.publish.call_args_list
        if str(c.args[0]).startswith("cio.context.gap.")
    ]
    assert gap_calls == []
