"""petrosa-cio#426: the spend endpoint and a gauge show whether CIO is in deterministic bypass."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_spend_ceiling_recovery_423 import ACTION, clock  # noqa: F401 (fixture)

from cio.apps.dashboard_api import router as dashboard_router
from cio.core.orchestrator import Orchestrator, _observe_llm_bypass
from cio.core.spend_tracker import LlmSpendTracker


def _orchestrator(env: dict[str, str] | None = None) -> Orchestrator:
    with (
        patch.dict("os.environ", env or {"NURSE_USE_LLM_REASONING": "true"}),
        patch("cio.core.orchestrator.ClientFactory.create", return_value=MagicMock()),
    ):
        return Orchestrator()


def _endpoint() -> dict:
    app = FastAPI()
    app.include_router(dashboard_router)
    response = TestClient(app).get("/api/dashboard/llm-spend")
    assert response.status_code == 200
    return response.json()


def _gauge(reason: str) -> float | None:
    values = {obs.attributes["reason"]: obs.value for obs in _observe_llm_bypass(None)}
    return values.get(reason)


@pytest.mark.asyncio
async def test_ceiling_bypass_shows_on_endpoint_and_gauge_then_clears(clock):  # noqa: F811
    orch = _orchestrator()
    assert _endpoint()["use_llm_reasoning"] is True
    assert _endpoint()["bypass_reason"] is None
    assert (_gauge("ceiling"), _gauge("env")) == (0.0, 0.0)

    clock.current = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
    LlmSpendTracker.instance().record(ACTION, 6_000_000, 0)
    with patch(
        "cio.core.alerting.manager.AlertManager.dispatch_critical_alert",
        new_callable=AsyncMock,
    ):
        await orch._check_spend_ceiling("breach")

    body = _endpoint()
    assert body["use_llm_reasoning"] is False
    assert body["bypass_reason"] == "ceiling"
    assert body["bypass_since"] is not None
    assert "ceiling_breached" in body  # the existing fields stay
    assert (_gauge("ceiling"), _gauge("env")) == (1.0, 0.0)

    # Later the same day the projection decays (ceiling_breached false) but the bypass holds:
    # the endpoint must still say so (the 2026-11-25 blind spot).
    clock.current = datetime(2026, 11, 25, 23, 0, tzinfo=UTC)
    await orch._check_spend_ceiling("same-day")
    body = _endpoint()
    assert body["ceiling_breached"] is False
    assert body["bypass_reason"] == "ceiling"

    clock.current = datetime(2026, 11, 26, 0, 30, tzinfo=UTC)
    await orch._check_spend_ceiling("next-day")
    body = _endpoint()
    assert body["use_llm_reasoning"] is True
    assert (body["bypass_reason"], body["bypass_since"]) == (None, None)
    assert (_gauge("ceiling"), _gauge("env")) == (0.0, 0.0)


def test_env_disabled_llm_shows_reason_env():
    orch = _orchestrator({"NURSE_USE_LLM_REASONING": "false"})
    body = _endpoint()
    assert body["use_llm_reasoning"] is False
    assert body["bypass_reason"] == "env"
    assert body["bypass_since"] is not None
    assert (_gauge("env"), _gauge("ceiling")) == (1.0, 0.0)
    assert orch.bypass_reason == "env"


def test_the_latest_orchestrator_is_reported():
    first = _orchestrator({"NURSE_USE_LLM_REASONING": "false"})
    live = _orchestrator()
    assert live.bypass_reason is None
    assert _endpoint()["bypass_reason"] is None
    assert _gauge("env") == 0.0
    # The older one is still alive (not merely garbage-collected) and still bypassed.
    assert first.bypass_reason == "env"
