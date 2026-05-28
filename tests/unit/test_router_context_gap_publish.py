"""P1.4-AC2.b (#132) — OutputRouter publishes context-gap audit events.

When the PreDecisionContext bundle carries one or more ContextGap entries
(populated by ContextBuilder during assembly), the router must emit one
``cio.context.gap.<surface>`` NATS message per gap at dispatch time so the
data-manager FR12 audit-trail consumer can persist them keyed by
``decision_id``. The decision itself must still proceed — gap publishing
is best-effort and must not block dispatch on a NATS hiccup.

Scope: producer-side only. The consumer (data-manager#179 follow-up)
subscribes to ``cio.context.gap.>`` and is out of scope for this ticket.
"""

from __future__ import annotations

import json
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
async def test_router_publishes_one_gap_event_per_surface():
    """AC2.b — every ContextGap on the bundle becomes one NATS publish on
    ``cio.context.gap.<surface>`` with a JSON payload containing
    decision_id, correlation_id, strategy_id, surface, reason, observed_at."""
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
    assert len(gap_calls) == 2
    subjects = {c.args[0] for c in gap_calls}
    assert subjects == {
        "cio.context.gap.evaluators",
        "cio.context.gap.characterization",
    }
    for call in gap_calls:
        payload = json.loads(call.args[1].decode())
        assert payload["decision_id"] == "decision-gap-132"
        assert payload["correlation_id"] == "corr-gap-132"
        assert payload["strategy_id"] == "strat-gap"
        assert payload["surface"] in {"evaluators", "characterization"}
        assert payload["reason"]
        assert "observed_at" in payload


@pytest.mark.asyncio
async def test_router_does_not_publish_when_no_gaps():
    """AC2.b — happy-path bundle (empty gaps list) emits zero context.gap
    messages; the rest of the dispatch is unaffected."""
    bundle = _make_bundle(with_gaps=False)
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


@pytest.mark.asyncio
async def test_router_swallows_publish_errors_on_gap_events():
    """AC2.b — gap publication is best-effort: a NATS publish exception
    must not propagate out of route() because the decision has already
    been committed at this point."""
    bundle = _make_bundle(with_gaps=True)
    ctx = _make_context(bundle=bundle)
    mock_nc = AsyncMock()

    async def _publish_side_effect(subject: str, payload: bytes) -> None:
        if subject.startswith("cio.context.gap."):
            raise RuntimeError("simulated NATS outage")

    mock_nc.publish.side_effect = _publish_side_effect

    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=AsyncMock(),
        ta_bot_url="http://ta-bot",
        decision_store=DecisionStore(),
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        # Must not raise.
        await router.route(ctx, _make_decision(ActionType.ADMIT))


@pytest.mark.asyncio
async def test_router_stores_bundle_on_decision_record():
    """AC4.a — DecisionStore.record(...) captures the bundle so
    /api/dashboard/decisions/recent can return it."""
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
