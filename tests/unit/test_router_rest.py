import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.router import OutputRouter
from cio.models import (
    ActionType,
    ActivationRecommendation,
    AppliedParamChange,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    ParamChangeDirection,
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
    context.decision_id = "test-decision-id"
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
    mock_cache.get = AsyncMock(return_value=None)  # not frozen → POST proceeds
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "rsi_extreme_reversal"  # registered TA_BOT strategy
    context.decision_id = "test-decision-id"
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
                "cio:freeze:rsi_extreme_reversal", "LOCKED", ttl=7200
            )


@pytest.mark.asyncio
async def test_output_router_rest_pause_strategy_freeze():
    """Verifies Redis freeze is set on successful PAUSE_STRATEGY."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)  # not frozen → POST proceeds
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "doji_reversal"  # registered TA_BOT strategy
    context.decision_id = "test-decision-id"
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
                "cio:freeze:doji_reversal", "LOCKED", ttl=1800
            )
            # Verify changed_by format (AC1)
            assert (
                mock_post.call_args[1]["json"]["changed_by"]
                == "petrosa-cio:doji_reversal"
            )


@pytest.mark.asyncio
async def test_output_router_rest_429_fallback_ttl():
    """Verifies Redis freeze uses default 3600s if json parsing fails on 429."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)  # not frozen → POST proceeds
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "rsi_extreme_reversal"  # registered TA_BOT strategy
    context.decision_id = "test-decision-id"
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
                "cio:freeze:rsi_extreme_reversal", "LOCKED", ttl=3600
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
    context.strategy_id = "hammer_reversal_pattern"  # registered TA_BOT strategy
    context.decision_id = "test-decision-id"
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
    context.strategy_id = "shooting_star_reversal"  # registered TA_BOT strategy
    context.decision_id = "test-decision-id"
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
            assert kwargs["json"]["changed_by"] == "petrosa-cio:shooting_star_reversal"


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
    context.strategy_id = "volume_surge_breakout"  # registered TA_BOT strategy
    context.decision_id = "test-decision-id"
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
            mock_cache.set.assert_called_with(
                "cio:freeze:volume_surge_breakout", "LOCKED", ttl=1
            )

            # 2. Test too high (100000 -> 86400)
            mock_response.json.return_value = {"retry_after": 100000}
            await router.route(context, decision)
            mock_cache.set.assert_called_with(
                "cio:freeze:volume_surge_breakout", "LOCKED", ttl=86400
            )

            # 3. Test float/string coercion
            mock_response.json.return_value = {"retry_after": "120.5"}
            await router.route(context, decision)
            mock_cache.set.assert_called_with(
                "cio:freeze:volume_surge_breakout", "LOCKED", ttl=120
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
    context.decision_id = "test-decision-id"
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


@pytest.mark.asyncio
async def test_output_router_rest_pause_strategy_skips_post_when_frozen():
    """AC4 (cio#169): When strategy is already frozen, no HTTP POST is sent.

    Prevents 429 storms caused by the LLM repeatedly deciding pause_strategy
    for the same strategy within the freeze window.
    """
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    # freeze is set — cache.get returns the LOCKED sentinel
    mock_cache.get = AsyncMock(return_value="LOCKED")

    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "ema_pullback_continuation"  # registered TA_BOT strategy
    context.decision_id = "dedup-test-id"
    context.correlation_id = "dedup-cid"

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
            await router.route(context, decision)
            mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_output_router_rest_modify_params_dry_run_shadow_log(caplog):
    """DRY_RUN branch of MODIFY_PARAMS logs [SHADOW MODE] and never POSTs."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        realtime_strategies_url="http://realtime",
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "momentum_pulse"
    context.decision_id = "test-decision-id"
    context.correlation_id = "modify-dry-run-id"

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

    with caplog.at_level("INFO"):
        with patch.dict(os.environ, {"DRY_RUN": "true"}):
            with patch.object(
                router.http_client, "post", new_callable=AsyncMock
            ) as mock_post:
                await router.route(context, decision)
                mock_post.assert_not_called()

    assert "[SHADOW MODE] Would have applied parameter change via REST" in caplog.text


@pytest.mark.asyncio
async def test_output_router_rest_modify_params_builds_params_dict_from_param_change():
    """When decision.param_change is set, the REST payload's `parameters`
    dict is built from it (covers the populated-params_dict branch)."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "momentum_pulse"
    context.decision_id = "test-decision-id"
    context.correlation_id = "modify-param-change-id"

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
        param_change=AppliedParamChange(
            strategy_id="momentum_pulse",
            timestamp=datetime.now(UTC),
            param="threshold",
            old_value=1.0,
            new_value=2.5,
            direction=ParamChangeDirection.INCREASE,
            reason="Test rationale",
        ),
    )

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(
            router.http_client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value.status_code = 200
            await router.route(context, decision)

            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            assert kwargs["json"]["parameters"] == {"threshold": 2.5}


@pytest.mark.asyncio
async def test_output_router_rest_modify_params_post_exception_logged(caplog):
    """An exception raised by http_client.post during MODIFY_PARAMS is caught
    and logged, not propagated."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "momentum_pulse"
    context.decision_id = "test-decision-id"
    context.correlation_id = "modify-exception-id"

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
            router.http_client,
            "post",
            new_callable=AsyncMock,
            side_effect=Exception("connection reset"),
        ):
            await router.route(context, decision)  # must not raise

    assert "Error applying parameter change via REST" in caplog.text


@pytest.mark.asyncio
async def test_output_router_rest_pause_strategy_429_handling():
    """PAUSE_STRATEGY: a 429 response triggers the rate-limit freeze path
    (mirrors the MODIFY_PARAMS 429 test, but for the PAUSE_STRATEGY branch)."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "doji_reversal"
    context.decision_id = "test-decision-id"
    context.correlation_id = "pause-429-id"

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
            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.json.return_value = {"retry_after": 900}
            mock_response.text = "Rate limit exceeded"
            mock_post.return_value = mock_response

            await router.route(context, decision)

            mock_cache.set.assert_called_with(
                "cio:freeze:doji_reversal", "LOCKED", ttl=900
            )


@pytest.mark.asyncio
async def test_output_router_rest_pause_strategy_post_exception_logged(caplog):
    """An exception raised by http_client.post during PAUSE_STRATEGY is
    caught and logged, not propagated."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "doji_reversal"
    context.decision_id = "test-decision-id"
    context.correlation_id = "pause-exception-id"

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
            router.http_client,
            "post",
            new_callable=AsyncMock,
            side_effect=Exception("connection reset"),
        ):
            await router.route(context, decision)  # must not raise

    assert "Error applying strategy pause via REST" in caplog.text


@pytest.mark.asyncio
async def test_output_router_rest_pause_strategy_empty_str_exception_logs_exc_type(
    caplog,
):
    """#209 (AC4): several exceptions (e.g. some httpx/connection-reset
    errors) stringify to '' — logging bare str(e) previously produced the
    misleading empty-tail "Error applying strategy pause via REST: " line
    with zero on-call signal. exc_type + a non-empty detail must always be
    present, mirroring the pattern already applied to context_builder.py's
    _fetch_strategy_stats / _fetch_regime (#197)."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "doji_reversal"
    context.decision_id = "test-decision-id"
    context.correlation_id = "pause-empty-str-id"

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

    class _EmptyStrConnectionResetError(Exception):
        def __str__(self):
            return ""

    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(
            router.http_client,
            "post",
            new_callable=AsyncMock,
            side_effect=_EmptyStrConnectionResetError(),
        ):
            await router.route(context, decision)  # must not raise

    assert "Error applying strategy pause via REST" in caplog.text
    assert "exc_type=_EmptyStrConnectionResetError" in caplog.text
    assert "detail=<empty>" in caplog.text


@pytest.mark.asyncio
async def test_output_router_rest_modify_params_body_failure_skips_freeze(caplog):
    """petrosa-cio#214 (defect 4, AC): a 200 response whose body reports
    ``{"success": false}`` (the shape ta_bot/realtime-strategies actually
    return on validation failure) must be treated as FAILED_TO_APPLY, not
    SUCCESS — and must NOT set the ``cio:freeze:`` lock, leaving CIO free
    to retry."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "momentum_pulse"
    context.decision_id = "test-decision-id"
    context.correlation_id = "body-failure-id"

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
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": False,
                "error": {"code": "VALIDATION_ERROR", "message": "bad param"},
            }
            mock_response.text = '{"success": false}'
            mock_post.return_value = mock_response

            await router.route(context, decision)

            assert "FAILED_TO_APPLY parameter change" in caplog.text
            mock_cache.set.assert_not_called()


@pytest.mark.asyncio
async def test_output_router_rest_pause_strategy_body_failure_skips_freeze(caplog):
    """petrosa-cio#214: same body-vs-status fix applied to PAUSE_STRATEGY."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "doji_reversal"
    context.decision_id = "test-decision-id"
    context.correlation_id = "pause-body-failure-id"

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
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": False,
                "error": {"code": "VALIDATION_ERROR", "message": "bad param"},
            }
            mock_response.text = '{"success": false}'
            mock_post.return_value = mock_response

            await router.route(context, decision)

            assert "FAILED_TO_APPLY strategy pause" in caplog.text
            mock_cache.set.assert_not_called()


@pytest.mark.asyncio
async def test_output_router_rest_modify_params_200_no_success_key_still_freezes():
    """Regression: a 2xx body with no ``"success"`` key at all (legacy
    producer response shape) must still be treated as SUCCESS — the new
    body check is additive, never more silent than the prior behavior."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value=None)
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
        cache=mock_cache,
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "momentum_pulse"
    context.decision_id = "test-decision-id"
    context.correlation_id = "legacy-body-id"

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
            mock_response.status_code = 200
            mock_response.json.return_value = {"ok": True}
            mock_post.return_value = mock_response

            await router.route(context, decision)

            mock_cache.set.assert_called_with(
                "cio:freeze:momentum_pulse", "LOCKED", ttl=1800
            )


@pytest.mark.asyncio
async def test_output_router_rest_fail_safe_create_task_exception_logged(caplog):
    """FAIL_SAFE: if scheduling the background REST pause task itself raises,
    the exception is caught and logged, not propagated."""
    mock_nc = AsyncMock()
    mock_vc = AsyncMock()
    router = OutputRouter(
        nats_client=mock_nc,
        vector_client=mock_vc,
        ta_bot_url="http://ta-bot",
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "shooting_star_reversal"
    context.decision_id = "test-decision-id"
    context.correlation_id = "fail-safe-exception-id"

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
        with patch(
            "cio.core.router.asyncio.create_task",
            side_effect=Exception("no running loop"),
        ):
            await router.route(context, decision)  # must not raise

    assert "Failed to fire fail-safe REST pause" in caplog.text
