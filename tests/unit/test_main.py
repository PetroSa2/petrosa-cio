import os
from unittest.mock import AsyncMock, patch

import pytest
from cio.main import main


@pytest.mark.asyncio
async def test_nats_subscription_with_wildcard():
    """Verify that the NATS listener subscribes to the correct subject with a wildcard."""
    # Create a mock for the NATS client that supports connect()
    mock_nc = AsyncMock()
    mock_nc.connect = AsyncMock()

    with patch.dict(os.environ, {"NATS_TOPIC_INTENTS": "cio.intent.trading"}), \
         patch("uvicorn.run"), \
         patch("cio.main.attach_logging_handler", return_value=True), \
         patch("cio.main.setup_telemetry", return_value=True), \
         patch("cio.main.NATSListener") as MockNATSListener, \
         patch("cio.main.NATS", return_value=mock_nc), \
         patch("redis.asyncio.from_url", new_callable=AsyncMock), \
         patch("cio.main.ClientFactory"), \
         patch("cio.main.ContextBuilder"), \
         patch("cio.main.Orchestrator"), \
         patch("cio.main.NurseEnforcer"), \
         patch("cio.main.OutputRouter"), \
         patch("cio.main.HeartbeatResponder"), \
         patch("prometheus_client.start_http_server"):

        mock_nats_listener = MockNATSListener.return_value
        mock_nats_listener.start = AsyncMock()

        # Mock the entire main loop to avoid SystemExit or real connections
        with patch("asyncio.create_task"), \
             patch("asyncio.Event") as mock_event_cls:
            
            mock_stop_event = mock_event_cls.return_value
            # Make the wait() return immediately
            mock_stop_event.wait = AsyncMock()
            
            await main()

        # Verify that the listener was started with the correct subject
        # cio.main.py appends .* if it's missing
        mock_nats_listener.start.assert_called_once_with(subject="cio.intent.trading.*")
