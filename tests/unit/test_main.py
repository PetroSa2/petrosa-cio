import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.main import main


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

    with patch.dict(os.environ, {"NATS_TOPIC_INTENTS": "cio.intent.trading"}), \
         patch("uvicorn.Config"), \
         patch("uvicorn.Server", return_value=mock_server), \
         patch("cio.main.attach_logging_handler", return_value=True), \
         patch("cio.main.setup_telemetry", return_value=True), \
         patch("cio.main.NATSListener") as MockNATSListener, \
         patch("cio.main.NATS", return_value=mock_nc), \
         patch("redis.asyncio.from_url", return_value=mock_redis), \
         patch("cio.main.ClientFactory"), \
         patch("cio.main.ContextBuilder") as MockContextBuilder, \
         patch("cio.main.Orchestrator"), \
         patch("cio.main.NurseEnforcer"), \
         patch("cio.main.OutputRouter") as MockOutputRouter, \
         patch("cio.main.HeartbeatResponder") as MockHeartbeatResponder, \
         patch("cio.main.HeartbeatPublisher") as MockHeartbeatPublisher, \
         patch("prometheus_client.start_http_server"):

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
