import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.apps.nurse import enforcer as enforcer_module
from cio.apps.nurse.enforcer import NurseEnforcer
from cio.clients import llm_client as llm_client_module
from cio.clients.llm_client import (
    LLM_CALL_TIMEOUT_SECONDS,
    LLM_RETRY_ATTEMPTS,
    LLM_RETRY_MAX_BACKOFF_SECONDS,
    LiteLLMClient,
)
from cio.core.orchestrator import SEQUENTIAL_LLM_STAGES
from cio.models import TIMEOUT_RETRY_RESULT, ActionType, TriggerContext


def test_audit_timeout_clamps_unsafe_configuration(monkeypatch):
    monkeypatch.setenv("LLM_AUDIT_TIMEOUT_MS", "20000")

    assert enforcer_module._resolve_audit_timeout_seconds() == 60.0


def test_audit_timeout_uses_default_for_invalid_configuration(monkeypatch):
    monkeypatch.setenv("LLM_AUDIT_TIMEOUT_MS", "not-a-duration")

    assert enforcer_module._resolve_audit_timeout_seconds() == 60.0


def test_llm_budget_fits_inside_audit_umbrella():
    worst_case_single_call = (
        LLM_CALL_TIMEOUT_SECONDS * LLM_RETRY_ATTEMPTS
        + LLM_RETRY_MAX_BACKOFF_SECONDS * (LLM_RETRY_ATTEMPTS - 1)
        + LLM_CALL_TIMEOUT_SECONDS
    )

    assert (
        worst_case_single_call * SEQUENTIAL_LLM_STAGES
        < enforcer_module.AUDIT_TIMEOUT_SECONDS
    )


@pytest.mark.asyncio
async def test_inner_llm_timeout_precedes_audit_timeout(monkeypatch):
    monkeypatch.setattr(enforcer_module, "AUDIT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(llm_client_module, "LLM_CALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(llm_client_module, "LLM_RETRY_MAX_BACKOFF_SECONDS", 0.01)

    async def slow_completion(**kwargs):
        await asyncio.sleep(kwargs["timeout"] * 10)

    import litellm

    completion = AsyncMock(side_effect=slow_completion)
    monkeypatch.setattr(litellm, "acompletion", completion)

    client = LiteLLMClient()
    orchestrator = MagicMock()

    async def run(_context):
        response = await client.complete("test", "system", {})
        assert response.error
        return TIMEOUT_RETRY_RESULT

    orchestrator.run = run
    context = MagicMock(spec=TriggerContext)
    context.correlation_id = "budget-ordering"
    context.strategy_id = "test-strategy"
    context.trigger_payload = {}

    started = time.perf_counter()
    decision = await NurseEnforcer(orchestrator).audit(context)
    elapsed = time.perf_counter() - started

    assert decision.action == ActionType.RETRY_SAFE
    assert elapsed < enforcer_module.AUDIT_TIMEOUT_SECONDS
    assert completion.await_count == 3
