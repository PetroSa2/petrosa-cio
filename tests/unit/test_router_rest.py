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
async def test_output_router_rest_429_fallback_ttl():
    """Verifies Redis freeze uses default 3600s if json parsing fails on 429."""
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
    context.correlation_id = "rest-429-fallback"

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
            mock_response.json.side_effect = Exception("Invalid JSON")
            mock_response.text = "Too many requests"
            mock_post.return_value = mock_response

            await router.route(context, decision)

            # Verify freeze key set with fallback TTL (3600)
            mock_cache.set.assert_called_with(
                "cio:freeze:test_strat", "LOCKED", ttl=3600
            )


@pytest.mark.asyncio
async def test_output_router_rest_cache_unavailable_warning(caplog):
    """Verifies warning is logged if cache is unavailable during success."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=None,  # No cache
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "no_cache_strat"
    context.correlation_id = "no-cache-id"

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

            assert "FREEZE_SKIPPED: cache unavailable" in caplog.text


@pytest.mark.asyncio
async def test_output_router_rest_fail_safe_identity():
    """Verifies FAIL_SAFE action uses per-strategy changed_by."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "fail_safe_strat"
    context.correlation_id = "fail-safe-id"

    decision = DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=ActionType.FAIL_SAFE,
        justification="Critical failure",
        thought_trace="Test",
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            await router.route(context, decision)

            # Wait a tiny bit for background task
            await asyncio.sleep(0.01)

            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            assert kwargs["json"]["changed_by"] == "petrosa-cio:fail_safe_strat"


@pytest.mark.asyncio
async def test_output_router_rest_429_clamping():
    """Verifies Redis freeze TTL is clamped between 1s and 86400s."""
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
    context.strategy_id = "clamp_strat"
    context.correlation_id = "rest-429-clamp"

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
            # 1. Test too low (0 -> 1)
            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.json.return_value = {"retry_after": 0}
            mock_post.return_value = mock_response
            await router.route(context, decision)
            mock_cache.set.assert_called_with("cio:freeze:clamp_strat", "LOCKED", ttl=1)

            # 2. Test too high (100000 -> 86400)
            mock_response.json.return_value = {"retry_after": 100000}
            await router.route(context, decision)
            mock_cache.set.assert_called_with(
                "cio:freeze:clamp_strat", "LOCKED", ttl=86400
            )

            # 3. Test float/string coercion
            mock_response.json.return_value = {"retry_after": "120.5"}
            await router.route(context, decision)
            mock_cache.set.assert_called_with(
                "cio:freeze:clamp_strat", "LOCKED", ttl=120
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
