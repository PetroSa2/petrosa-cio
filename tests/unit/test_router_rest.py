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


@pytest.mark.asyncio
async def test_output_router_rest_modify_params_active():
    """Verifies REST POST is called for MODIFY_PARAMS when DRY_RUN is false."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()

    # Initialize with explicit URLs
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        realtime_strategies_url="http://realtime",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "momentum_pulse"  # TA_BOT
    context.correlation_id = "rest-active-id"

    decision = DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=ActionType.MODIFY_PARAMS,
        justification="Test",
        thought_trace="Test",
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value.status_code = 200
            await router.route(context, decision)

            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert "http://ta-bot/api/v1/strategies/momentum_pulse/config" in args[0]
            assert kwargs["json"]["changed_by"] == "petrosa-cio:momentum_pulse"

            # Verify freeze key set
            mock_cache.set.assert_called_with(
                "cio:freeze:momentum_pulse", "LOCKED", ttl=1800
            )


@pytest.mark.asyncio
async def test_output_router_rest_429_handling():
    """Verifies Redis freeze is set with retry_after on 429 response."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "test_strat"
    context.correlation_id = "rest-429-id"

    decision = DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=ActionType.MODIFY_PARAMS,
        justification="Test",
        thought_trace="Test",
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.json.return_value = {"retry_after": 7200}
            mock_response.text = "Rate limit exceeded"
            mock_post.return_value = mock_response

            await router.route(context, decision)

            # Verify freeze key set with TTL from response
            mock_cache.set.assert_called_with(
                "cio:freeze:test_strat", "LOCKED", ttl=7200
            )


@pytest.mark.asyncio
async def test_output_router_rest_pause_strategy_freeze():
    """Verifies Redis freeze is set on successful PAUSE_STRATEGY."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "pause_strat"
    context.correlation_id = "rest-pause-id"

    decision = DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=ActionType.PAUSE_STRATEGY,
        justification="Test",
        thought_trace="Test",
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value.status_code = 200
            await router.route(context, decision)

            # Verify freeze key set with 1800s TTL (AC3)
            mock_cache.set.assert_called_with(
                "cio:freeze:pause_strat", "LOCKED", ttl=1800
            )
            # Verify changed_by format (AC1)
            assert (
                mock_post.call_args[1]["json"]["changed_by"]
                == "petrosa-cio:pause_strat"
            )


@pytest.mark.asyncio
async def test_output_router_rest_dry_run():
    """Verifies REST POST is NOT called when DRY_RUN is true."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        realtime_strategies_url="http://realtime",
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "orderbook_skew"  # REALTIME
    context.correlation_id = "rest-dryrun-id"

    decision = DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=ActionType.PAUSE_STRATEGY,
        justification="Test",
        thought_trace="Test",
    )

    with patch.dict(os.environ, {"DRY_RUN": "true"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            await router.route(context, decision)
            mock_post.assert_not_called()
