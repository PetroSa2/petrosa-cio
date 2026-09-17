"""petrosa-cio#200: router-level coverage for TargetServiceResolver integration.

Verifies the three REST dispatch call sites (MODIFY_PARAMS, PAUSE_STRATEGY,
FAIL_SAFE) in OutputRouter.route():
- skip the REST call (no crash, no misrouted POST) when the strategy resolves
  to ServiceType.UNKNOWN instead of silently defaulting to TA_BOT, and
- prefer `trigger_payload["metadata"]["strategy_id"]` (the canonical id) over
  the top-level `context.strategy_id` (which may be a human display name)
  when resolving the target service.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.router import OutputRouter
from cio.models import (
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    RegimeFit,
    TriggerContext,
)


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
        justification="Test",
        thought_trace="Test",
    )


def _make_router(**overrides) -> OutputRouter:
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)  # not frozen -> POST proceeds
    defaults = {
        "nats_client": AsyncMock(),
        "vector_client": AsyncMock(),
        "ta_bot_url": "http://ta-bot",
        "realtime_strategies_url": "http://realtime",
        "cache": mock_cache,
    }
    defaults.update(overrides)
    return OutputRouter(**defaults)


def _make_context(strategy_id: str, trigger_payload: dict | None = None):
    context = MagicMock(spec=TriggerContext)
    context.strategy_id = strategy_id
    context.decision_id = "test-decision-id"
    context.correlation_id = "service-resolution-cid"
    context.trigger_payload = (
        trigger_payload if trigger_payload is not None else {"symbol": "BTCUSDT"}
    )
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", [ActionType.MODIFY_PARAMS, ActionType.PAUSE_STRATEGY]
)
async def test_unroutable_strategy_skips_rest_post_without_crashing(action, caplog):
    """AC3: an unresolvable strategy skips the REST call instead of guessing
    TA_BOT — no exception, no POST."""
    router = _make_router()
    context = _make_context("totally_unregistered_strategy")
    decision = _make_decision(action)

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            await router.route(context, decision)
            mock_post.assert_not_called()

    assert "UNROUTABLE_STRATEGY" in caplog.text


@pytest.mark.asyncio
async def test_unroutable_strategy_skips_fail_safe_rest_post(caplog):
    """FAIL_SAFE branch: unroutable strategy skips the REST double-lock POST
    but the NATS failure signal path is untouched (no crash)."""
    router = _make_router()
    context = _make_context("totally_unregistered_strategy")
    decision = _make_decision(ActionType.FAIL_SAFE)

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            await router.route(context, decision)
            await asyncio.sleep(0.01)
            mock_post.assert_not_called()

    assert "UNROUTABLE_STRATEGY" in caplog.text


@pytest.mark.asyncio
async def test_display_name_strategy_routes_to_realtime_strategies():
    """petrosa-cio#200 reported bug: the top-level display name alone
    (no metadata override) still resolves correctly via normalization +
    alias, and routes to realtime_strategies_url, not ta_bot_url."""
    router = _make_router()
    context = _make_context("Iceberg Order Detector")
    decision = _make_decision(ActionType.PAUSE_STRATEGY)

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value.status_code = 200
            await router.route(context, decision)

            mock_post.assert_called_once()
            args, _ = mock_post.call_args
            assert args[0].startswith("http://realtime/")


@pytest.mark.asyncio
async def test_metadata_strategy_id_preferred_over_display_name():
    """Scope bullet: prefer metadata.strategy_id (canonical) over the
    top-level display name when both are present in trigger_payload."""
    router = _make_router()
    context = _make_context(
        "Iceberg Order Detector",
        trigger_payload={
            "symbol": "BTCUSDT",
            "strategy_id": "Iceberg Order Detector",
            "metadata": {"strategy_id": "iceberg_detector"},
        },
    )
    decision = _make_decision(ActionType.PAUSE_STRATEGY)

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value.status_code = 200
            await router.route(context, decision)

            mock_post.assert_called_once()
            args, _ = mock_post.call_args
            assert args[0].startswith("http://realtime/")


@pytest.mark.asyncio
async def test_unroutable_strategy_dry_run_still_skips_and_logs(caplog):
    """The UNKNOWN-skip check happens before the DRY_RUN branch, so shadow
    mode does not mask the routing failure."""
    router = _make_router()
    context = _make_context("totally_unregistered_strategy")
    decision = _make_decision(ActionType.MODIFY_PARAMS)

    with patch.dict(os.environ, {"DRY_RUN": "true"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            await router.route(context, decision)
            mock_post.assert_not_called()

    assert "UNROUTABLE_STRATEGY" in caplog.text
    assert "Would have applied parameter change via REST" not in caplog.text
