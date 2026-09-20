"""Tests for ExecutionEventsConsumer (petrosa_k8s#1130).

Covers:
- start/stop NATS subscription lifecycle (mirrors AlertsConsumer conventions).
- `_is_position_closed`: filled+closed and position_force_closed_no_stops
  are closing signals; filled+partial and unknown event types are not.
- `_handle_message`: fires `record_exit` + `remove_position` on a closing
  event, using `client_order_id` as the position_id (falling back to
  strategy_id when absent), and is a no-op otherwise.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.core.execution_events_consumer import (
    EXECUTION_EVENTS_SUBJECT_PATTERN,
    ExecutionEventsConsumer,
    _is_position_closed,
)


def _make_nats_msg(subject: str, data: dict) -> MagicMock:
    msg = MagicMock()
    msg.subject = subject
    msg.data = json.dumps(data).encode()
    return msg


def _make_nats_client() -> AsyncMock:
    nc = AsyncMock()
    nc.subscribe = AsyncMock()
    return nc


# ---------------------------------------------------------------------------
# _is_position_closed
# ---------------------------------------------------------------------------


def test_filled_closed_is_closing():
    assert _is_position_closed({"event_type": "filled", "position_status": "closed"})


def test_filled_partial_is_not_closing():
    assert not _is_position_closed(
        {"event_type": "filled", "position_status": "partial"}
    )


def test_filled_without_position_status_is_not_closing():
    assert not _is_position_closed({"event_type": "filled"})


def test_force_closed_no_stops_is_unconditionally_closing():
    assert _is_position_closed({"event_type": "position_force_closed_no_stops"})


def test_placed_event_is_not_closing():
    assert not _is_position_closed({"event_type": "placed"})


def test_rejected_event_is_not_closing():
    assert not _is_position_closed({"event_type": "rejected"})


# ---------------------------------------------------------------------------
# start / stop lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumer_starts_and_subscribes():
    nc = _make_nats_client()
    consumer = ExecutionEventsConsumer(nats_client=nc)
    await consumer.start()

    nc.subscribe.assert_awaited_once()
    call_args = nc.subscribe.call_args
    assert call_args.args[0] == EXECUTION_EVENTS_SUBJECT_PATTERN == "execution.events.>"


@pytest.mark.asyncio
async def test_consumer_stop_unsubscribes():
    nc = _make_nats_client()
    mock_sub = AsyncMock()
    nc.subscribe = AsyncMock(return_value=mock_sub)

    consumer = ExecutionEventsConsumer(nats_client=nc)
    await consumer.start()
    await consumer.stop()

    mock_sub.unsubscribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_consumer_stop_idempotent_when_not_started():
    nc = _make_nats_client()
    consumer = ExecutionEventsConsumer(nats_client=nc)
    await consumer.stop()  # must not raise


# ---------------------------------------------------------------------------
# _handle_message — closing events retire the position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_close_fires_record_exit_and_remove_position():
    nc = _make_nats_client()
    tracker = AsyncMock()
    tracker.record_exit = AsyncMock()
    review_loop = MagicMock()
    review_loop.remove_position = MagicMock()

    consumer = ExecutionEventsConsumer(
        nats_client=nc, portfolio_tracker=tracker, position_review_loop=review_loop
    )
    msg = _make_nats_msg(
        "execution.events.iceberg_detector",
        {
            "event_type": "filled",
            "strategy_id": "iceberg_detector",
            "position_status": "closed",
            "client_order_id": "cio-position-id-abc123",
        },
    )
    await consumer._handle_message(msg)

    tracker.record_exit.assert_awaited_once_with(strategy_id="iceberg_detector")
    review_loop.remove_position.assert_called_once_with(
        "iceberg_detector", "cio-position-id-abc123"
    )


@pytest.mark.asyncio
async def test_missing_client_order_id_falls_back_to_strategy_id():
    nc = _make_nats_client()
    tracker = AsyncMock()
    tracker.record_exit = AsyncMock()
    review_loop = MagicMock()

    consumer = ExecutionEventsConsumer(
        nats_client=nc, portfolio_tracker=tracker, position_review_loop=review_loop
    )
    msg = _make_nats_msg(
        "execution.events.momentum_v3",
        {"event_type": "position_force_closed_no_stops", "strategy_id": "momentum_v3"},
    )
    await consumer._handle_message(msg)

    review_loop.remove_position.assert_called_once_with("momentum_v3", "momentum_v3")


@pytest.mark.asyncio
async def test_partial_close_does_not_retire_position():
    nc = _make_nats_client()
    tracker = AsyncMock()
    tracker.record_exit = AsyncMock()
    review_loop = MagicMock()

    consumer = ExecutionEventsConsumer(
        nats_client=nc, portfolio_tracker=tracker, position_review_loop=review_loop
    )
    msg = _make_nats_msg(
        "execution.events.iceberg_detector",
        {
            "event_type": "filled",
            "strategy_id": "iceberg_detector",
            "position_status": "partial",
            "client_order_id": "cio-position-id-abc123",
        },
    )
    await consumer._handle_message(msg)

    tracker.record_exit.assert_not_awaited()
    review_loop.remove_position.assert_not_called()


@pytest.mark.asyncio
async def test_placed_event_does_not_retire_position():
    nc = _make_nats_client()
    tracker = AsyncMock()
    tracker.record_exit = AsyncMock()

    consumer = ExecutionEventsConsumer(nats_client=nc, portfolio_tracker=tracker)
    msg = _make_nats_msg(
        "execution.events.iceberg_detector",
        {"event_type": "placed", "strategy_id": "iceberg_detector"},
    )
    await consumer._handle_message(msg)

    tracker.record_exit.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_strategy_id_is_a_noop_not_an_error():
    nc = _make_nats_client()
    tracker = AsyncMock()
    tracker.record_exit = AsyncMock()

    consumer = ExecutionEventsConsumer(nats_client=nc, portfolio_tracker=tracker)
    msg = _make_nats_msg(
        "execution.events.unknown",
        {"event_type": "filled", "position_status": "closed", "strategy_id": ""},
    )
    await consumer._handle_message(msg)

    tracker.record_exit.assert_not_awaited()


@pytest.mark.asyncio
async def test_works_without_position_review_loop_wired():
    """SIGNAL_ARBITRATION_ENABLED=false means position_review_loop is None."""
    nc = _make_nats_client()
    tracker = AsyncMock()
    tracker.record_exit = AsyncMock()

    consumer = ExecutionEventsConsumer(
        nats_client=nc, portfolio_tracker=tracker, position_review_loop=None
    )
    msg = _make_nats_msg(
        "execution.events.iceberg_detector",
        {
            "event_type": "filled",
            "strategy_id": "iceberg_detector",
            "position_status": "closed",
        },
    )
    await consumer._handle_message(msg)  # must not raise

    tracker.record_exit.assert_awaited_once_with(strategy_id="iceberg_detector")


@pytest.mark.asyncio
async def test_drops_invalid_json_gracefully():
    nc = _make_nats_client()
    tracker = AsyncMock()
    tracker.record_exit = AsyncMock()

    consumer = ExecutionEventsConsumer(nats_client=nc, portfolio_tracker=tracker)
    msg = MagicMock()
    msg.subject = "execution.events.iceberg_detector"
    msg.data = b"not-json"

    await consumer._handle_message(msg)  # must not raise

    tracker.record_exit.assert_not_awaited()
