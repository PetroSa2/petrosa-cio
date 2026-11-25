"""petrosa-cio#423: the spend ceiling must not trip on early-morning calls, and a
ceiling-triggered bypass must end when the UTC day rolls over."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cio.core.spend_tracker as spend_tracker
from cio.core.orchestrator import Orchestrator
from cio.core.spend_tracker import LlmSpendTracker
from tests.unit.test_orchestrator_persona_concurrency import _make_context

ACTION = "PETROSA_PROMPT_ACTION_CLASSIFIER"


class _Clock(datetime):
    current = datetime(2026, 11, 25, 0, 2, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.current


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(spend_tracker, "datetime", _Clock)
    _Clock.current = datetime(2026, 11, 25, 0, 2, tzinfo=UTC)
    tracker = LlmSpendTracker.instance()
    tracker.reset_for_test(ceiling_usd=5.0)
    yield _Clock
    tracker.reset_for_test()


def _orchestrator() -> Orchestrator:
    with patch("cio.core.orchestrator.ClientFactory.create", return_value=MagicMock()):
        return Orchestrator()


def test_early_morning_calls_do_not_breach_the_ceiling(clock):
    tracker = LlmSpendTracker.instance()
    # The 2026-11-25 production calls: ~14.7k input / 475 output tokens per action call.
    for _ in range(8):
        tracker.record(ACTION, 14_663 // 8, 475 // 8)
    tracker.record(ACTION, 14_663, 475)
    breached, total, projected = tracker.check_ceiling()
    # Unfloored, 2 minutes into the day this projected total * 720 (> $5).
    assert total * 720 > 5.0
    assert breached is False
    assert projected == pytest.approx(total * 24)


def test_a_sustained_spend_rate_above_the_ceiling_still_breaches(clock):
    clock.current = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
    tracker = LlmSpendTracker.instance()
    # $1.50 in 6 h projects to $6/day.
    tracker.record(ACTION, 6_000_000, 0)
    breached, _, projected = tracker.check_ceiling()
    assert breached is True
    assert projected == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_ceiling_bypass_holds_for_the_day_and_ends_on_the_next(clock):
    orch = _orchestrator()
    clock.current = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
    LlmSpendTracker.instance().record(ACTION, 6_000_000, 0)
    with patch(
        "cio.core.alerting.manager.AlertManager.dispatch_critical_alert",
        new_callable=AsyncMock,
    ):
        await orch._check_spend_ceiling("breach")
    assert orch.use_llm_reasoning is False

    # Same day, spend stopped: the projection decays below the ceiling, the bypass holds.
    clock.current = datetime(2026, 11, 25, 23, 0, tzinfo=UTC)
    await orch._check_spend_ceiling("same-day")
    assert orch.use_llm_reasoning is False

    # Next UTC day: LLM reasoning is restored.
    clock.current = datetime(2026, 11, 26, 0, 5, tzinfo=UTC)
    await orch._check_spend_ceiling("next-day")
    assert orch.use_llm_reasoning is True
    assert orch._ceiling_triggered_bypass is False


class _Stop(Exception):
    pass


@pytest.mark.asyncio
async def test_run_rechecks_the_ceiling_while_the_bypass_is_ceiling_triggered():
    orch = _orchestrator()
    orch.use_llm_reasoning = False
    orch._ceiling_triggered_bypass = True
    orch._check_spend_ceiling = AsyncMock(side_effect=_Stop)
    with pytest.raises(_Stop) as err:
        await orch.run(_make_context())
    assert isinstance(err.value, _Stop)
    orch._check_spend_ceiling.assert_awaited_once_with("test-persona-concurrency")
