import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.apps.nurse.enforcer import NurseEnforcer
from cio.models import ActionType, TriggerContext


@pytest.mark.asyncio
async def test_nurse_enforcer_timeout_triggers_retry_safe():
    """
    Validates that the NurseEnforcer triggers a RETRY_SAFE decision
    if the underlying orchestrator takes longer than the timeout.
    Uses an Event to avoid real wall-clock sleep and flakiness.
    """
    # 1. Setup Mock Orchestrator that waits on an event that never fires
    mock_orchestrator = MagicMock()
    timeout_event = asyncio.Event()

    async def slow_run(*args, **kwargs):
        await timeout_event.wait()
        return MagicMock()  # Should not reach here

    mock_orchestrator.run = slow_run

    enforcer = NurseEnforcer(orchestrator=mock_orchestrator)

    # 2. Setup Mock Context
    mock_context = MagicMock(spec=TriggerContext)
    mock_context.correlation_id = "test-timeout-id"
    mock_context.strategy_id = "test-strategy"
    mock_context.trigger_payload = {}

    # 3. Execute Audit
    decision = await enforcer.audit(mock_context)

    # 4. Assertions
    assert decision.action == ActionType.RETRY_SAFE
    assert "TIMEOUT_GUARD" in decision.justification
    assert decision.thought_trace == "TIMEOUT_ENFORCEMENT"


@pytest.mark.asyncio
async def test_nurse_enforcer_fast_audit_passes():
    """
    Validates that the NurseEnforcer passes the decision through
    if the orchestrator is fast (under the timeout).
    """
    # 1. Setup Mock Orchestrator that is fast
    mock_orchestrator = MagicMock()
    mock_decision = MagicMock()
    mock_decision.action = ActionType.EXECUTE

    mock_orchestrator.run = AsyncMock(return_value=mock_decision)

    enforcer = NurseEnforcer(orchestrator=mock_orchestrator)

    # 2. Setup Mock Context
    mock_context = MagicMock(spec=TriggerContext)
    mock_context.correlation_id = "test-fast-id"
    mock_context.strategy_id = "test-strategy"
    mock_context.trigger_payload = {}

    # 3. Execute Audit
    decision = await enforcer.audit(mock_context)

    # 4. Assertions
    assert decision.action == ActionType.EXECUTE
    assert decision == mock_decision


@pytest.mark.asyncio
async def test_nurse_enforcer_critical_failure_triggers_fail_safe():
    """
    Validates that the NurseEnforcer triggers a FAIL_SAFE decision
    if the underlying orchestrator raises an exception.
    """
    # 1. Setup Mock Orchestrator that raises an error
    mock_orchestrator = MagicMock()
    mock_orchestrator.run = AsyncMock(side_effect=ValueError("Simulated crash"))

    enforcer = NurseEnforcer(orchestrator=mock_orchestrator)

    # 2. Setup Mock Context
    mock_context = MagicMock(spec=TriggerContext)
    mock_context.correlation_id = "test-crash-id"
    mock_context.strategy_id = "test-strategy"
    mock_context.trigger_payload = {}

    # 3. Execute Audit
    decision = await enforcer.audit(mock_context)

    # 4. Assertions
    assert decision.action == ActionType.FAIL_SAFE
    assert "CRITICAL_FAILURE" in decision.justification
    assert decision.thought_trace == "CRITICAL_FAILURE_ENFORCEMENT"
