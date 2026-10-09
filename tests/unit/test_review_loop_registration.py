"""The review loop registers a position only for a really dispatched EXECUTE (the registration leak fix)."""

import gc
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core import position_review_loop as prl
from cio.core.authority import ActionAuthority, AuthorityStore
from cio.core.execution_events_consumer import ExecutionEventsConsumer
from cio.core.position_review_loop import PositionKey, PositionReviewLoop
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


class _Runner:
    async def __call__(self, key, reason):
        return None


def _decision(action):
    return DecisionResult(
        hard_blocked=action == ActionType.BLOCK,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=action,
        justification="test",
        thought_trace="test",
    )


def _context(position_id="pos-1", strategy_id="iceberg_detector"):
    context = MagicMock(spec=TriggerContext)
    context.strategy_id = strategy_id
    context.decision_id = "decision"
    context.correlation_id = "correlation"
    context.position_id = position_id
    context.trigger_payload = {"symbol": "BTCUSDT"}
    return context


def _router(loop):
    return OutputRouter(
        nats_client=AsyncMock(),
        vector_client=AsyncMock(),
        ta_bot_url="http://ta-bot",
        realtime_strategies_url="http://realtime",
        cache=AsyncMock(),
        position_review_loop=loop,
    )


async def _route(router, context, action, *, dry_run="false", signal=True):
    with patch.dict(os.environ, {"DRY_RUN": dry_run}):
        with (
            patch(
                "cio.core.router.TradeEngineTranslator.to_legacy_signal",
                return_value={"symbol": "BTCUSDT"} if signal else None,
            ),
            patch.object(router.http_client, "post", new_callable=AsyncMock),
            patch.object(router.http_client, "put", new_callable=AsyncMock),
        ):
            await router.route(context, _decision(action))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        ActionType.SKIP,
        ActionType.PAUSE_STRATEGY,
        ActionType.BLOCK,
        ActionType.MODIFY_PARAMS,
        ActionType.FAIL_SAFE,
    ],
)
async def test_skip_pause_block_and_other_decisions_never_register(action):
    loop = PositionReviewLoop(runner=_Runner())
    await _route(_router(loop), _context(), action)
    assert loop.active_positions() == []


@pytest.mark.asyncio
async def test_an_execute_registers_the_position_under_its_own_position_id():
    loop = PositionReviewLoop(runner=_Runner())
    router = _router(loop)
    await _route(router, _context("pos-1"), ActionType.EXECUTE)
    await _route(router, _context("pos-2"), ActionType.EXECUTE)
    # two positions of one strategy stay two keys: no merge under the strategy id
    assert loop.active_positions() == [
        PositionKey("iceberg_detector", "pos-1"),
        PositionKey("iceberg_detector", "pos-2"),
    ]


@pytest.mark.asyncio
async def test_an_execute_that_is_not_dispatched_does_not_register():
    loop = PositionReviewLoop(runner=_Runner())
    router = _router(loop)
    await _route(router, _context(), ActionType.EXECUTE, dry_run="true")  # dry run
    await _route(
        router, _context(), ActionType.EXECUTE, signal=False
    )  # no signal built
    assert loop.active_positions() == []


@pytest.mark.asyncio
async def test_an_execute_without_a_position_id_is_not_registered_and_says_so(caplog):
    loop = PositionReviewLoop(runner=_Runner())
    await _route(_router(loop), _context(position_id=None), ActionType.EXECUTE)
    assert loop.active_positions() == []  # never the strategy id as a fallback key
    assert "REVIEW_LOOP_NOT_REGISTERED" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", [ActionAuthority.DISABLED, ActionAuthority.OPERATOR_APPROVAL_REQUIRED]
)
async def test_an_execute_the_authority_store_downgrades_or_diverts_does_not_register(
    state,
):
    loop = PositionReviewLoop(runner=_Runner())
    router = _router(loop)
    store = AuthorityStore()
    store.set_state(ActionType.EXECUTE, state, operator_id="op", reason="test")
    router.authority_store = store
    await _route(router, _context(), ActionType.EXECUTE)
    assert loop.active_positions() == []


@pytest.mark.asyncio
async def test_the_close_event_retires_the_position_and_a_repeat_is_harmless():
    loop = PositionReviewLoop(runner=_Runner())
    await _route(_router(loop), _context("pos-1"), ActionType.EXECUTE)
    consumer = ExecutionEventsConsumer(
        nats_client=MagicMock(),
        portfolio_tracker=AsyncMock(),
        position_review_loop=loop,
    )
    close = MagicMock()
    close.subject = "execution.events.iceberg_detector"
    close.data = json.dumps(
        {
            "event_type": "filled",
            "position_status": "closed",
            "strategy_id": "iceberg_detector",
            "client_order_id": "pos-1",
        }
    ).encode()
    await consumer._handle_message(close)
    assert loop.active_positions() == []
    await consumer._handle_message(close)  # idempotent: the same close again, no error
    assert loop.active_positions() == []


@pytest.mark.asyncio
async def test_a_restart_starts_empty_and_registers_only_real_executes():
    first = PositionReviewLoop(runner=_Runner())
    await _route(_router(first), _context("pos-old"), ActionType.EXECUTE)
    assert len(first.active_positions()) == 1
    # a restart: a new process builds a new loop and router; the registry is in memory only
    second = PositionReviewLoop(runner=_Runner())
    router = _router(second)
    assert second.active_positions() == []
    for _ in range(5):  # intents that end skip or pause leave nothing behind
        await _route(router, _context(f"pos-{_}"), ActionType.SKIP)
        await _route(router, _context(f"pos-{_}"), ActionType.PAUSE_STRATEGY)
    assert second.active_positions() == []
    await _route(router, _context("pos-new"), ActionType.EXECUTE)
    assert second.active_positions() == [PositionKey("iceberg_detector", "pos-new")]


# --- the registry: idempotent, bounded, observable ------------------------------------------------


def test_add_is_idempotent_and_remove_is_idempotent():
    loop = PositionReviewLoop(runner=_Runner())
    assert loop.add_position("s", "p") is True
    assert loop.add_position("s", "p") is False  # same position twice: one entry
    assert len(loop.active_positions()) == 1
    assert loop.remove_position("s", "p") is True
    assert loop.remove_position("s", "p") is False  # already retired
    assert loop.remove_position("nope", "nope") is False
    assert loop.active_positions() == []


def test_the_registry_is_bounded_and_evicts_the_oldest_registration():
    loop = PositionReviewLoop(runner=_Runner(), max_positions=3)
    before = prl.cio_review_loop_evicted._value.get()
    for n in range(5):
        loop.add_position("s", f"p{n}")
    assert [k.position_id for k in loop.active_positions()] == ["p2", "p3", "p4"]
    assert prl.cio_review_loop_evicted._value.get() - before == 2


def test_the_cap_comes_from_the_environment_with_a_fallback(monkeypatch):
    monkeypatch.delenv("CIO_REVIEW_LOOP_MAX_POSITIONS", raising=False)
    assert prl.max_positions_from_env() == 200
    monkeypatch.setenv("CIO_REVIEW_LOOP_MAX_POSITIONS", "7")
    assert prl.max_positions_from_env() == 7
    assert PositionReviewLoop(runner=_Runner())._max_positions == 7
    for bad in ("0", "-3", "x", ""):
        monkeypatch.setenv("CIO_REVIEW_LOOP_MAX_POSITIONS", bad)
        assert prl.max_positions_from_env() == 200


def test_the_positions_gauge_follows_the_registry():
    gc.collect()
    loop = PositionReviewLoop(runner=_Runner())
    base = prl.cio_review_loop_positions._value.get()
    loop.add_position("s", "a")
    loop.add_position("s", "b")
    assert prl.cio_review_loop_positions._value.get() == base + 2
    loop.remove_position("s", "a")
    assert prl.cio_review_loop_positions._value.get() == base + 1
    loop.add_position("s", "b")  # a repeat does not move it
    assert prl.cio_review_loop_positions._value.get() == base + 1
    # the OTLP observable gauge of the same name reports the live registry too
    observed = list(prl._observe_positions(None))
    assert observed and observed[0].value >= 1
