"""Cost-share pre-filter in the reasoning loop (petrosa-cio#296, rule 19): before any LLM call."""

import os
from unittest.mock import AsyncMock, patch

import pytest

from cio.core.orchestrator import Orchestrator
from cio.models import ActionType, CodeEngineResult, NetEvGate
from tests.unit.test_net_ev_gate import _context


def _code_result(skip: bool) -> CodeEngineResult:
    return CodeEngineResult(
        recommended_sl_pct=0.06,
        recommended_tp_pct=0.04,
        net_ev_gate=NetEvGate(
            result="fail",
            reason="net_ev_lcb_below_zero",
            method="posterior",
            s_eff=0.06,
            take_profit_pct=0.04,
            cost_total=0.03,
            cost_share=0.5,
            p_ref=0.5,
            cost_share_limit=-0.17,
            cost_share_skip=skip,
        ),
    )


async def _run(mode: str, skip: bool):
    env = {"NURSE_USE_LLM_REASONING": "true", "CIO_COST_SHARE_PREFILTER": mode}
    with (
        patch.dict(os.environ, env),
        patch("cio.core.orchestrator.CodeEngine") as engine,
        patch("cio.core.orchestrator.RegimeAnalyst") as regime,
        patch("cio.core.orchestrator.StrategyAssessor"),
        patch("cio.core.orchestrator.ActionClassifier"),
    ):
        engine.run.return_value = _code_result(skip)
        regime.return_value.classify = AsyncMock(side_effect=RuntimeError("stop here"))
        decision = await Orchestrator().run(_context())
        return decision, regime.return_value.classify


@pytest.mark.asyncio
async def test_enforce_skips_before_any_llm_call():
    decision, classify = await _run("enforce", skip=True)
    assert decision.action == ActionType.SKIP
    assert "cost_share_prefilter" in decision.justification
    classify.assert_not_called()


@pytest.mark.asyncio
async def test_log_only_by_default_does_not_skip():
    decision, classify = await _run("log_only", skip=True)
    classify.assert_called()  # the LLM stage still runs


@pytest.mark.asyncio
async def test_enforce_without_a_breach_proceeds():
    _, classify = await _run("enforce", skip=False)
    classify.assert_called()


@pytest.mark.asyncio
async def test_off_never_skips():
    _, classify = await _run("off", skip=True)
    classify.assert_called()
