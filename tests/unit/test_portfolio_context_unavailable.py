import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_engine import build_test_context

from cio.core.assembler import (
    PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE,
    DecisionAssembler,
)
from cio.core.engine import CodeEngine
from cio.core.health_evaluator import (
    FALLBACK_TRACE_MARKERS,
    HEALTHY,
    UNHEALTHY,
    CIOHealthEvaluator,
)
from cio.core.health_evaluator import (
    PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE as HEALTH_PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE,
)
from cio.core.orchestrator import Orchestrator
from cio.models import (
    ActionType,
    ActivationRecommendation,
    CodeEngineResult,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    RegimeFit,
)
from cio.models.enums import RejectionSource


def test_portfolio_context_unavailable_rejection_source_value():
    assert RejectionSource.PORTFOLIO_CONTEXT_UNAVAILABLE.value == (
        "portfolio_context_unavailable"
    )
    assert RejectionSource.CONTEXT_UNAVAILABLE.value == "context_unavailable"


def test_code_engine_portfolio_unavailable_blocks_without_fake_numbers():
    result = CodeEngine.run(
        build_test_context(drawdown=0.0, portfolio_state_available=False)
    )

    assert result.hard_blocked is True
    assert result.block_context_fallback is True
    assert result.block_reason.startswith("PORTFOLIO_CONTEXT_UNAVAILABLE: ")
    assert "%" not in result.block_reason
    assert "Global drawdown" not in result.block_reason
    assert result.recommended_sl_pct is None


def test_code_engine_portfolio_unavailable_includes_gap_reason():
    context = build_test_context(portfolio_state_available=False)
    context.pre_decision_context.gaps = []
    assert "(unknown)" in CodeEngine.run(context).block_reason


def test_code_engine_portfolio_unavailable_metric_split():
    with (
        patch("cio.core.engine.RISK_GATE_CONTEXT_FALLBACK") as fallback,
        patch("cio.core.engine.RISK_GATE_REAL_BREACH") as real_breach,
    ):
        CodeEngine.run(build_test_context(portfolio_state_available=False))
        fallback.add.assert_called_once_with(1)
        real_breach.add.assert_not_called()

        fallback.reset_mock()
        real_breach.reset_mock()
        CodeEngine.run(
            build_test_context(drawdown=0.15, portfolio_state_available=True)
        )
        fallback.add.assert_not_called()
        real_breach.add.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_orchestrator_fallback_block_skips_action_classifier():
    with (
        patch.dict(os.environ, {"NURSE_USE_LLM_REASONING": "true"}),
        patch("cio.core.orchestrator.ActionClassifier") as classifier,
    ):
        classifier.return_value.classify = AsyncMock()
        decision = await Orchestrator(llm_client=MagicMock()).run(
            build_test_context(portfolio_state_available=False)
        )

    classifier.return_value.classify.assert_not_awaited()
    assert decision.action == ActionType.BLOCK
    assert decision.rejection_source == RejectionSource.PORTFOLIO_CONTEXT_UNAVAILABLE
    assert decision.thought_trace == PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE
    assert decision.hard_block_reason.startswith("PORTFOLIO_CONTEXT_UNAVAILABLE: ")
    assert "%" not in decision.justification
    assert decision.activation_recommendation == ActivationRecommendation.RUN
    assert decision.regime_confidence == ConfidenceLevel.LOW


@pytest.mark.asyncio
async def test_orchestrator_fallback_block_skips_classifier_in_bypass_mode():
    with (
        patch.dict(os.environ, {"NURSE_USE_LLM_REASONING": "false"}),
        patch("cio.core.orchestrator.ActionClassifier") as classifier,
    ):
        classifier.return_value.classify = AsyncMock()
        decision = await Orchestrator(llm_client=MagicMock()).run(
            build_test_context(portfolio_state_available=False)
        )

    classifier.return_value.classify.assert_not_awaited()
    assert decision.rejection_source == RejectionSource.PORTFOLIO_CONTEXT_UNAVAILABLE


@pytest.mark.asyncio
async def test_orchestrator_real_breach_still_calls_classifier():
    with (
        patch.dict(os.environ, {"NURSE_USE_LLM_REASONING": "true"}),
        patch("cio.core.orchestrator.ActionClassifier") as classifier,
    ):
        classifier.return_value.classify = AsyncMock(
            return_value=DecisionResult(
                hard_blocked=True,
                hard_block_reason="real breach",
                ev_passes=False,
                cost_viable=False,
                regime_confidence=ConfidenceLevel.LOW,
                regime_fit=RegimeFit.NEUTRAL,
                strategy_health=HealthStatus.HEALTHY,
                activation_recommendation=ActivationRecommendation.RUN,
                action=ActionType.BLOCK,
            )
        )
        await Orchestrator(llm_client=MagicMock()).run(
            build_test_context(drawdown=0.15, portfolio_state_available=True)
        )

    classifier.return_value.classify.assert_awaited_once()


def test_assembler_real_hard_block_unchanged():
    decision = DecisionAssembler.assemble(
        context=build_test_context(),
        code_result=CodeEngineResult(
            hard_blocked=True,
            block_reason="Global drawdown 15.00% exceeds limit 10.00%.",
            block_context_fallback=False,
        ),
        regime_result=build_test_context().regime,
        strategy_result=MagicMock(
            regime_fit=RegimeFit.NEUTRAL,
            health=HealthStatus.HEALTHY,
            activation_recommendation=ActivationRecommendation.RUN,
        ),
    )

    assert decision.rejection_source is None
    assert decision.thought_trace == (
        "Code Engine safety gate triggered. Bypassing all LLM logic."
    )
    assert decision.justification == (
        "Hard blocked by engine: Global drawdown 15.00% exceeds limit 10.00%."
    )


@pytest.mark.asyncio
async def test_router_audit_copy_carries_rejection_source():
    from cio.core.router import OutputRouter

    nats_client = AsyncMock()
    vector_client = AsyncMock()
    router = OutputRouter(nats_client=nats_client, vector_client=vector_client)
    context = MagicMock()
    context.strategy_id = "strategy"
    context.decision_id = "decision"
    context.correlation_id = "correlation"
    context.trigger_payload = {}
    decision = DecisionResult(
        hard_blocked=True,
        hard_block_reason="PORTFOLIO_CONTEXT_UNAVAILABLE: outage",
        ev_passes=False,
        cost_viable=False,
        regime_confidence=ConfidenceLevel.LOW,
        regime_fit=RegimeFit.NEUTRAL,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=ActionType.BLOCK,
        justification="PORTFOLIO_CONTEXT_UNAVAILABLE: outage",
        thought_trace=PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE,
        rejection_source=RejectionSource.PORTFOLIO_CONTEXT_UNAVAILABLE,
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        await router.route(context, decision)
    await router.close()

    audit_payload = next(
        payload
        for call in nats_client.publish.call_args_list
        if call.args[0] == "cio.decision.audit.block"
        for payload in [call.args[1]]
    )
    assert b'"rejection_source": "portfolio_context_unavailable"' in audit_payload
    assert b'"hard_block_reason": "PORTFOLIO_CONTEXT_UNAVAILABLE: outage"' in (
        audit_payload
    )


@pytest.mark.asyncio
async def test_portfolio_context_unavailable_dominance_yields_specific_unhealthy():
    evaluator = CIOHealthEvaluator(MagicMock(), missing_context_threshold=0.5)
    for _ in range(4):
        await evaluator._on_decision(
            MagicMock(
                data=b'{"action":"block","thought_trace":"'
                b'PORTFOLIO_CONTEXT_UNAVAILABLE"}'
            )
        )
    await evaluator._on_decision(
        MagicMock(data=b'{"action":"execute","thought_trace":"solid trace"}')
    )

    verdict, reason = evaluator.evaluate()
    assert verdict == UNHEALTHY
    assert reason.startswith(
        "portfolio context unavailable on 80% of recent decisions (4/5)"
    )


@pytest.mark.asyncio
async def test_portfolio_context_unavailable_minority_does_not_trip_specific_reason():
    evaluator = CIOHealthEvaluator(MagicMock(), missing_context_threshold=0.5)
    await evaluator._on_decision(
        MagicMock(
            data=b'{"action":"block","thought_trace":"PORTFOLIO_CONTEXT_UNAVAILABLE"}'
        )
    )
    for _ in range(4):
        await evaluator._on_decision(
            MagicMock(data=b'{"action":"execute","thought_trace":"solid trace"}')
        )

    verdict, _ = evaluator.evaluate()
    assert verdict == HEALTHY


def test_portfolio_trace_constant_matches_assembler():
    assert (
        HEALTH_PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE
        == PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE
    )
    assert HEALTH_PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE == (
        RejectionSource.PORTFOLIO_CONTEXT_UNAVAILABLE.value.upper()
    )
    assert HEALTH_PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE in FALLBACK_TRACE_MARKERS
