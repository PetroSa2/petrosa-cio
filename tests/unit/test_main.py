import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.main import main


def test_litellm_logger_silenced_at_import_time():
    """#192: the `LiteLLM` logger must be raised to WARNING so its internal
    INFO chatter (e.g. "LiteLLM completion() model=...") no longer floods
    Grafana/Loki as unparseable "unknown"-severity noise. This is applied at
    module import time in cio/main.py, before any server/event loop starts.
    """
    assert logging.getLogger("LiteLLM").level == logging.WARNING


@pytest.mark.asyncio
async def test_nats_subscription_with_wildcard():
    """Verify that the NATS listener subscribes to the correct subject with a wildcard."""
    # Create a mock for the NATS client that supports connect()
    mock_nc = AsyncMock()
    mock_nc.connect = AsyncMock()

    # Create a mock for the Redis client
    mock_redis = AsyncMock()
    mock_redis.close = AsyncMock()

    # Create a mock for uvicorn Server
    mock_server = MagicMock()
    mock_server.serve = AsyncMock()
    mock_server.shutdown = AsyncMock()

    with (
        patch.dict(os.environ, {"NATS_TOPIC_INTENTS": "cio.intent.trading"}),
        patch("uvicorn.Config"),
        patch("uvicorn.Server", return_value=mock_server),
        patch(
            "cio.main.attach_logging_handler", return_value=True
        ) as mock_attach_handler,
        patch("cio.main.setup_telemetry", return_value=True),
        patch("cio.main.NATSListener") as MockNATSListener,
        patch("cio.main.NATS", return_value=mock_nc),
        patch("redis.asyncio.from_url", return_value=mock_redis),
        patch("cio.main.ClientFactory"),
        patch("cio.main.ContextBuilder") as MockContextBuilder,
        patch("cio.main.Orchestrator"),
        patch("cio.main.NurseEnforcer"),
        patch("cio.main.OutputRouter") as MockOutputRouter,
        patch("cio.main.HeartbeatResponder") as MockHeartbeatResponder,
        patch("cio.main.HeartbeatPublisher") as MockHeartbeatPublisher,
        patch("cio.main.PositionReviewLoop") as MockPositionReviewLoop,
    ):
        mock_nats_listener = MockNATSListener.return_value
        mock_nats_listener.start = AsyncMock()
        mock_nats_listener.stop = AsyncMock()

        mock_heartbeat_responder = MockHeartbeatResponder.return_value
        mock_heartbeat_responder.start = AsyncMock()
        mock_heartbeat_responder.stop = AsyncMock()

        mock_heartbeat_publisher = MockHeartbeatPublisher.return_value
        mock_heartbeat_publisher.start = AsyncMock()
        mock_heartbeat_publisher.stop = AsyncMock()

        mock_router = MockOutputRouter.return_value
        mock_router.close = AsyncMock()

        mock_builder = MockContextBuilder.return_value
        mock_builder.close = AsyncMock()

        # #175: PositionReviewLoop.start()/stop() spawn a real asyncio task
        # via the cadence loop; mock it like the other main.py collaborators
        # so this test stays focused on the NATS wildcard subscription.
        mock_position_review_loop = MockPositionReviewLoop.return_value
        mock_position_review_loop.start = AsyncMock()
        mock_position_review_loop.stop = AsyncMock()

        # Mock the entire main loop to avoid SystemExit or real connections
        with patch("asyncio.Event") as mock_event_cls:
            created_tasks = []

            def _capture_task(coro):
                coro.close()
                task = MagicMock()
                created_tasks.append(task)
                return task

            with patch("asyncio.create_task", side_effect=_capture_task):
                mock_stop_event = mock_event_cls.return_value
                # Make the wait() return immediately
                mock_stop_event.wait = AsyncMock(return_value=None)

                await main()

        # Verify that the listener was started with the correct subject
        # cio.main.py appends .> if it's missing (following Petrosa NATS contract)
        mock_nats_listener.start.assert_called_once_with(subject="cio.intent.trading.>")

    # #192: stdout logs must be structured JSON (not the text formatter) so
    # Grafana/Loki can derive a real severity token instead of "unknown".
    mock_attach_handler.assert_called_once_with(use_json_format=True)

    # #209 (AC3): nc.connect() must wire structured reconnect callbacks and
    # an infinite retry budget so a transient transport drop (e.g. the
    # 2026-09-18 incident's ConnectionResetError during reconnection) is
    # logged through our structured logger instead of leaking as a raw,
    # correlation-id-less traceback, and never permanently closes the
    # connection out from under an otherwise-healthy process.
    connect_kwargs = mock_nc.connect.call_args.kwargs
    assert connect_kwargs["max_reconnect_attempts"] == -1
    assert connect_kwargs["reconnect_time_wait"] == 2
    for cb_name in ("error_cb", "disconnected_cb", "reconnected_cb", "closed_cb"):
        assert callable(connect_kwargs[cb_name]), f"{cb_name} must be wired"


@pytest.mark.asyncio
async def test_nats_error_cb_logs_structured_exc_type_not_bare_str(caplog):
    """#209 (AC3): the wired error_cb must log exc_type + a non-empty detail
    (never a bare str(e), which is '' for some transport exceptions — same
    empty-tail failure class fixed in context_builder.py by #197)."""
    caplog.set_level(logging.WARNING, logger="cio-strategist")

    mock_nc = AsyncMock()
    mock_nc.connect = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.close = AsyncMock()
    mock_server = MagicMock()
    mock_server.serve = AsyncMock()
    mock_server.shutdown = AsyncMock()

    with (
        patch.dict(os.environ, {"NATS_TOPIC_INTENTS": "cio.intent.trading"}),
        patch("uvicorn.Config"),
        patch("uvicorn.Server", return_value=mock_server),
        patch("cio.main.attach_logging_handler", return_value=True),
        patch("cio.main.setup_telemetry", return_value=True),
        patch("cio.main.NATSListener") as MockNATSListener,
        patch("cio.main.NATS", return_value=mock_nc),
        patch("redis.asyncio.from_url", return_value=mock_redis),
        patch("cio.main.ClientFactory"),
        patch("cio.main.ContextBuilder") as MockContextBuilder,
        patch("cio.main.Orchestrator"),
        patch("cio.main.NurseEnforcer"),
        patch("cio.main.OutputRouter") as MockOutputRouter,
        patch("cio.main.HeartbeatResponder") as MockHeartbeatResponder,
        patch("cio.main.HeartbeatPublisher") as MockHeartbeatPublisher,
        patch("cio.main.PositionReviewLoop") as MockPositionReviewLoop,
    ):
        MockNATSListener.return_value.start = AsyncMock()
        MockNATSListener.return_value.stop = AsyncMock()
        MockHeartbeatResponder.return_value.start = AsyncMock()
        MockHeartbeatResponder.return_value.stop = AsyncMock()
        MockHeartbeatPublisher.return_value.start = AsyncMock()
        MockHeartbeatPublisher.return_value.stop = AsyncMock()
        MockOutputRouter.return_value.close = AsyncMock()
        MockContextBuilder.return_value.close = AsyncMock()
        MockPositionReviewLoop.return_value.start = AsyncMock()
        MockPositionReviewLoop.return_value.stop = AsyncMock()

        with patch("asyncio.Event") as mock_event_cls:

            def _capture_task(coro):
                coro.close()
                return MagicMock()

            with patch("asyncio.create_task", side_effect=_capture_task):
                mock_event_cls.return_value.wait = AsyncMock(return_value=None)
                await main()

    error_cb = mock_nc.connect.call_args.kwargs["error_cb"]

    class _EmptyStrConnectionError(Exception):
        def __str__(self):
            return ""

    await error_cb(_EmptyStrConnectionError())

    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "NATS_TRANSPORT_ERROR" in r.message
    ]
    assert warning_records, "expected a structured NATS_TRANSPORT_ERROR WARNING"
    body = warning_records[0].message
    assert "exc_type=_EmptyStrConnectionError" in body
    assert "detail=<empty>" in body
    assert warning_records[0].audit_exempt is True
    assert warning_records[0].transient is True
