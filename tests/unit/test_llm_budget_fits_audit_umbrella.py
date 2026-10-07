import asyncio
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
    monkeypatch.delenv("LLM_PRIMARY_ATTEMPTS_WITH_FALLBACK", raising=False)
    inner_timeout = AsyncMock(side_effect=asyncio.TimeoutError)
    monkeypatch.setattr(
        llm_client_module, "_acompletion_with_timeout", inner_timeout
    )
    inner_timeout_completed = asyncio.Event()
    outer_timeout_reached = asyncio.Event()

    async def dispatch_outer_timeout(*_args, **_kwargs):
        outer_timeout_reached.set()

    monkeypatch.setattr(
        enforcer_module.AlertManager,
        "dispatch_critical_alert",
        dispatch_outer_timeout,
    )

    client = LiteLLMClient()
    orchestrator = MagicMock()

    async def run(_context):
        response = await client.complete("test", "system", {})
        assert response.error
        inner_timeout_completed.set()
        return TIMEOUT_RETRY_RESULT

    orchestrator.run = run
    context = MagicMock(spec=TriggerContext)
    context.correlation_id = "budget-ordering"
    context.strategy_id = "test-strategy"
    context.trigger_payload = {}

    decision = await NurseEnforcer(orchestrator).audit(context)

    assert decision.action == ActionType.RETRY_SAFE
    assert inner_timeout_completed.is_set()
    await asyncio.sleep(0)
    assert not outer_timeout_reached.is_set()
    # Default routes are distinct (haiku primary, gpt-4o-mini fallback), so a
    # primary timeout goes straight to the fallback after one attempt.
    assert inner_timeout.await_count == llm_client_module._primary_attempts(True) + 1
    assert inner_timeout.await_count == 2
