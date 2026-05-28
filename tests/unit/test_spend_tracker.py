"""Unit tests for LlmSpendTracker (FR63 — petrosa-data-manager#170)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cio.apps.dashboard_api import router as dashboard_router
from cio.core.spend_tracker import (
    DECISION_TYPE_LABELS,
    LlmSpendTracker,
    PeriodSpend,
    _tokens_to_usd,
)

# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------


def test_tokens_to_usd_zero():
    assert _tokens_to_usd(0, 0) == 0.0


def test_tokens_to_usd_only_input():
    # 1M input tokens @ default $0.25/1M = $0.25
    cost = _tokens_to_usd(1_000_000, 0)
    assert abs(cost - 0.25) < 1e-9


def test_tokens_to_usd_only_output():
    # 1M output tokens @ default $1.25/1M = $1.25
    cost = _tokens_to_usd(0, 1_000_000)
    assert abs(cost - 1.25) < 1e-9


# ---------------------------------------------------------------------------
# PeriodSpend
# ---------------------------------------------------------------------------


def test_period_spend_record_and_bucket():
    p = PeriodSpend(period_date=date.today())
    p.record("PETROSA_PROMPT_REGIME_CLASSIFIER", 1000, 500)
    label = DECISION_TYPE_LABELS["PETROSA_PROMPT_REGIME_CLASSIFIER"]
    assert label in p.buckets
    assert p.buckets[label].input_tokens == 1000
    assert p.buckets[label].output_tokens == 500
    assert p.buckets[label].call_count == 1


def test_period_spend_unknown_prompt_id_falls_back_to_raw_id():
    p = PeriodSpend(period_date=date.today())
    p.record("UNKNOWN_PROMPT", 100, 50)
    assert "UNKNOWN_PROMPT" in p.buckets


def test_period_spend_ceiling_not_breached_at_zero():
    p = PeriodSpend(period_date=date.today(), ceiling_usd_per_day=5.0)
    assert not p.ceiling_breached()


def test_period_spend_ceiling_breached_when_projected_exceeds():
    p = PeriodSpend(period_date=date.today(), ceiling_usd_per_day=0.0)
    p.record("PETROSA_PROMPT_ACTION_CLASSIFIER", 1000, 100)
    assert p.ceiling_breached()


# ---------------------------------------------------------------------------
# LlmSpendTracker singleton
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_tracker():
    LlmSpendTracker.instance().reset_for_test()
    yield
    LlmSpendTracker.instance().reset_for_test()


def test_tracker_record_accumulates():
    tracker = LlmSpendTracker.instance()
    tracker.record("PETROSA_PROMPT_REGIME_CLASSIFIER", 1000, 200)
    tracker.record("PETROSA_PROMPT_REGIME_CLASSIFIER", 500, 100)
    snap = tracker.period_snapshot()
    # Only one bucket
    assert len(snap["buckets"]) == 1
    b = snap["buckets"][0]
    assert b["input_tokens"] == 1500
    assert b["output_tokens"] == 300
    assert b["call_count"] == 2


def test_tracker_multiple_decision_types():
    tracker = LlmSpendTracker.instance()
    tracker.record("PETROSA_PROMPT_REGIME_CLASSIFIER", 1000, 100)
    tracker.record("PETROSA_PROMPT_STRATEGY_ASSESSOR", 800, 200)
    tracker.record("PETROSA_PROMPT_ACTION_CLASSIFIER", 600, 150)
    snap = tracker.period_snapshot()
    assert len(snap["buckets"]) == 3


def test_tracker_check_ceiling_no_breach():
    tracker = LlmSpendTracker.instance()
    tracker.record("PETROSA_PROMPT_ACTION_CLASSIFIER", 100, 50)
    breached, total, projected = tracker.check_ceiling()
    assert not breached
    assert total > 0


def test_tracker_check_ceiling_breached_on_zero_ceiling():
    tracker = LlmSpendTracker.instance()
    tracker.reset_for_test(ceiling_usd=0.0)
    tracker.record("PETROSA_PROMPT_ACTION_CLASSIFIER", 100_000, 50_000)
    breached, total, projected = tracker.check_ceiling()
    assert breached


def test_tracker_snapshot_keys():
    snap = LlmSpendTracker.instance().period_snapshot()
    expected = {
        "period_date",
        "ceiling_usd_per_day",
        "total_cost_usd",
        "projected_daily_usd",
        "ceiling_breached",
        "distance_to_ceiling_usd",
        "buckets",
    }
    assert expected.issubset(snap.keys())


# ---------------------------------------------------------------------------
# /api/dashboard/llm-spend route
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)
    return app


@pytest.fixture
def client():
    return TestClient(_make_app())


def test_llm_spend_route_ok(client):
    resp = client.get("/api/dashboard/llm-spend")
    assert resp.status_code == 200
    data = resp.json()
    assert "ceiling_usd_per_day" in data
    assert "total_cost_usd" in data
    assert "buckets" in data


def test_llm_spend_route_reflects_recorded_spend(client):
    LlmSpendTracker.instance().record("PETROSA_PROMPT_REGIME_CLASSIFIER", 10_000, 5_000)
    resp = client.get("/api/dashboard/llm-spend")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_cost_usd"] > 0
    assert len(data["buckets"]) == 1
    assert data["buckets"][0]["decision_type"] == "regime_classification"


# ---------------------------------------------------------------------------
# Orchestrator._check_spend_ceiling — FR63 / AC4 + AC5 integration
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from cio.core.orchestrator import Orchestrator  # noqa: E402


def _build_orchestrator() -> Orchestrator:
    """Construct an Orchestrator with a stub LLM client so the personas don't
    touch real settings / network — we only exercise _check_spend_ceiling.
    """
    with patch("cio.core.orchestrator.ClientFactory.create", return_value=MagicMock()):
        return Orchestrator()


@pytest.mark.asyncio
async def test_orchestrator_check_spend_ceiling_breach_switches_to_bypass():
    """AC4: ceiling breach → use_llm_reasoning=False + alert dispatched."""
    orch = _build_orchestrator()
    assert orch.use_llm_reasoning is True

    with (
        patch.object(
            LlmSpendTracker.instance(),
            "check_ceiling",
            return_value=(True, 12.5, 25.0),
        ),
        patch(
            "cio.core.alerting.manager.AlertManager.dispatch_critical_alert",
            new_callable=AsyncMock,
        ) as mock_alert,
    ):
        await orch._check_spend_ceiling(correlation_id="cid-1")

    assert orch.use_llm_reasoning is False
    assert orch._ceiling_triggered_bypass is True
    mock_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_check_spend_ceiling_recovery_restores_llm_mode():
    """AC5: on period roll (not breached) after the ceiling triggered the bypass,
    LLM reasoning is restored.
    """
    orch = _build_orchestrator()
    # Simulate the prior breach: bypass-active state.
    orch.use_llm_reasoning = False
    orch._ceiling_triggered_bypass = True

    with patch.object(
        LlmSpendTracker.instance(),
        "check_ceiling",
        return_value=(False, 0.0, 0.0),
    ):
        await orch._check_spend_ceiling(correlation_id="cid-recover")

    assert orch.use_llm_reasoning is True
    assert orch._ceiling_triggered_bypass is False


@pytest.mark.asyncio
async def test_orchestrator_check_spend_ceiling_alert_failure_does_not_propagate():
    """AC4 robustness: an AlertManager exception must not stop the bypass switch."""
    orch = _build_orchestrator()

    with (
        patch.object(
            LlmSpendTracker.instance(),
            "check_ceiling",
            return_value=(True, 50.0, 100.0),
        ),
        patch(
            "cio.core.alerting.manager.AlertManager.dispatch_critical_alert",
            new_callable=AsyncMock,
            side_effect=RuntimeError("alert backend down"),
        ),
    ):
        # Must not raise even though the alert dispatch fails.
        await orch._check_spend_ceiling(correlation_id="cid-alert-fail")

    assert orch.use_llm_reasoning is False
    assert orch._ceiling_triggered_bypass is True


@pytest.mark.asyncio
async def test_orchestrator_check_spend_ceiling_no_breach_when_already_llm():
    """No-op path: ceiling not breached and LLM mode is already on."""
    orch = _build_orchestrator()
    assert orch.use_llm_reasoning is True

    with patch.object(
        LlmSpendTracker.instance(),
        "check_ceiling",
        return_value=(False, 0.5, 1.0),
    ):
        await orch._check_spend_ceiling(correlation_id="cid-noop")

    assert orch.use_llm_reasoning is True
    assert orch._ceiling_triggered_bypass is False
