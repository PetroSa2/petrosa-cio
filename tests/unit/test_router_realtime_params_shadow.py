"""CIO_REALTIME_PARAMS_MODE: MODIFY_PARAMS aimed at realtime strategies is shadowed by default (like the pauses of #324)."""

import logging
import os
from datetime import UTC, datetime
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
from cio.models.enums import ParamChangeDirection
from cio.models.strategy import AppliedParamChange

REALTIME = "iceberg_detector"  # registered realtime-strategies strategy
TA_BOT = "momentum_pulse"  # registered TA-bot strategy


def _decision():
    return DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=ActionType.MODIFY_PARAMS,
        justification="tighten the threshold",
        thought_trace="test",
        param_change=AppliedParamChange(
            strategy_id=REALTIME,
            timestamp=datetime.now(UTC),
            param="min_confidence",
            old_value=0.5,
            new_value=0.6,
            direction=ParamChangeDirection.INCREASE,
            reason="noisy",
        ),
    )


def _context(strategy_id):
    context = MagicMock(spec=TriggerContext)
    context.strategy_id = strategy_id
    context.decision_id = "decision"
    context.correlation_id = "correlation"
    context.trigger_payload = {"symbol": "BTCUSDT"}
    return context


def _router(params_mode, *, pause_mode=None, cache=None):
    env = {}
    if params_mode is not None:
        env["CIO_REALTIME_PARAMS_MODE"] = params_mode
    if pause_mode is not None:
        env["CIO_REALTIME_PAUSE_MODE"] = pause_mode
    with patch.dict(os.environ, env, clear=False):
        if params_mode is None:
            os.environ.pop("CIO_REALTIME_PARAMS_MODE", None)
        if pause_mode is None:
            os.environ.pop("CIO_REALTIME_PAUSE_MODE", None)
        return OutputRouter(
            nats_client=AsyncMock(),
            vector_client=AsyncMock(),
            ta_bot_url="http://ta-bot",
            realtime_strategies_url="http://realtime",
            cache=cache if cache is not None else AsyncMock(),
        )


@pytest.mark.parametrize("raw", ["bogus", "", "  APPLYY "])
def test_an_invalid_realtime_params_mode_falls_back_to_shadow_with_a_warning(
    raw, caplog
):
    caplog.set_level(logging.WARNING)
    assert _router(raw).realtime_params_mode == "shadow"
    assert (
        "CIO_REALTIME_PARAMS_MODE" in caplog.text
        and "invalid; using shadow" in caplog.text
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "shadow"),
        ("shadow", "shadow"),
        (" SHADOW ", "shadow"),
        ("apply", "apply"),
        (" Apply", "apply"),
    ],
)
def test_the_realtime_params_mode_is_read_from_the_environment(raw, expected, caplog):
    caplog.set_level(logging.WARNING)
    assert _router(raw).realtime_params_mode == expected
    assert "invalid; using shadow" not in caplog.text


@pytest.mark.asyncio
async def test_shadow_mode_makes_no_http_call_and_sets_no_freeze_for_a_realtime_strategy(
    caplog,
):
    caplog.set_level(logging.INFO)
    cache = AsyncMock()
    router = _router(None, cache=cache)  # the default is shadow
    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with (
            patch.object(router.http_client, "post", new_callable=AsyncMock) as post,
            patch.object(router.http_client, "put", new_callable=AsyncMock) as put,
        ):
            await router.route(_context(REALTIME), _decision())
    post.assert_not_awaited()
    put.assert_not_awaited()
    cache.set.assert_not_awaited()  # no freeze key
    record = next(r for r in caplog.records if "SHADOW_PARAMS" in r.getMessage())
    assert record.getMessage() == "SHADOW_PARAMS would change realtime strategy params"
    assert record.strategy_id == REALTIME and record.symbol == "BTCUSDT"
    assert (record.param, record.new_value) == ("min_confidence", 0.6)
    assert record.proposed_change == {
        "parameters": {"min_confidence": 0.6},
        "changed_by": f"petrosa-cio:{REALTIME}",
        "reason": "tighten the threshold",
        "validate_only": False,
    }
    assert record.target_url == f"http://realtime/api/v1/strategies/{REALTIME}/config"


@pytest.mark.asyncio
async def test_apply_mode_keeps_todays_realtime_post_and_freeze():
    cache = AsyncMock()
    router = _router("apply", cache=cache)
    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(router.http_client, "post", new_callable=AsyncMock) as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {"success": True}
            await router.route(_context(REALTIME), _decision())
    post.assert_awaited_once()
    assert (
        post.call_args.args[0] == f"http://realtime/api/v1/strategies/{REALTIME}/config"
    )
    cache.set.assert_awaited_with(f"cio:freeze:{REALTIME}", "LOCKED", ttl=1800)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "shadow", "apply"])
async def test_ta_bot_param_changes_are_unchanged_in_every_mode(mode):
    cache = AsyncMock()
    router = _router(mode, cache=cache)
    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(router.http_client, "post", new_callable=AsyncMock) as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {"success": True}
            await router.route(_context(TA_BOT), _decision())
    post.assert_awaited_once()
    assert post.call_args.args[0] == f"http://ta-bot/api/v1/strategies/{TA_BOT}/config"
    cache.set.assert_awaited_with(f"cio:freeze:{TA_BOT}", "LOCKED", ttl=1800)


@pytest.mark.asyncio
async def test_the_params_mode_and_the_pause_mode_are_independent():
    # params applied while pauses stay shadowed...
    router = _router("apply", pause_mode="shadow")
    assert (router.realtime_params_mode, router.realtime_pause_mode) == (
        "apply",
        "shadow",
    )
    # ... and params shadowed while pauses are applied
    router = _router("shadow", pause_mode="apply")
    assert (router.realtime_params_mode, router.realtime_pause_mode) == (
        "shadow",
        "apply",
    )
    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(router.http_client, "post", new_callable=AsyncMock) as post:
            await router.route(_context(REALTIME), _decision())
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unroutable_strategy_is_still_skipped_and_logged_in_shadow_mode(
    caplog,
):
    caplog.set_level(logging.INFO)
    router = _router(None)
    with patch.dict(os.environ, {"DRY_RUN": "false"}):
        with patch.object(router.http_client, "post", new_callable=AsyncMock) as post:
            await router.route(_context("totally_unregistered_strategy"), _decision())
    post.assert_not_awaited()
    assert "UNROUTABLE_STRATEGY" in caplog.text
    assert "SHADOW_PARAMS" not in caplog.text
