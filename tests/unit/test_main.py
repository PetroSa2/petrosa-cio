
import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from cio.main import main

@pytest.mark.asyncio
async def test_nats_subscription_with_wildcard():
    """Verify that the NATS listener subscribes to the correct subject with a wildcard."""
    with patch.dict(os.environ, {"NATS_TOPIC_INTENTS": "cio.intent.trading"}), \
         patch("uvicorn.run"), \
         patch("cio.main.NATSListener") as MockNATSListener, \
         patch("nats.connect", new_callable=AsyncMock) as mock_nats_connect, \
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
        mock_nats_connect.return_value = AsyncMock()

        # Create a mock for the stop_event that is set after a short delay
        async def set_stop_event():
            await asyncio.sleep(0.1)
            # Find the stop_event in the main's local scope and set it
            for task in asyncio.all_tasks():
                if task.get_coro().__name__ == 'main':
                    for stack_frame in task.get_stack():
                        if 'stop_event' in stack_frame.f_locals:
                            stack_frame.f_locals['stop_event'].set()
                            break
                    break

        asyncio.create_task(set_stop_event())
        await main()

        # Verify that the listener was started with the correct subject
        mock_nats_listener.start.assert_called_once_with(subject="cio.intent.trading.*")
