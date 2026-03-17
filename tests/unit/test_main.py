import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from cio.main import main


@pytest.mark.asyncio
async def test_nats_subscription_with_wildcard():
    """Verify that the NATS listener subscribes to the correct subject with a wildcard."""
    
    with patch.dict(os.environ, {"NATS_TOPIC_INTENTS": "cio.intent.trading"}), \
         patch("cio.main.NATS") as MockNATSClass, \
         patch("redis.asyncio.from_url") as mock_redis_from_url, \
         patch("cio.main.AsyncRedisCache"), \
         patch("cio.main.ClientFactory"), \
         patch("cio.main.ContextBuilder") as MockContextBuilder, \
         patch("cio.main.Orchestrator"), \
         patch("cio.main.NurseEnforcer"), \
         patch("cio.main.OutputRouter") as MockOutputRouter, \
         patch("cio.main.NATSListener") as MockNATSListener, \
         patch("cio.main.HeartbeatResponder") as MockHeartbeatResponder, \
         patch("prometheus_client.start_http_server"), \
         patch("uvicorn.Server") as MockUvicornServer:
         
        # Configure the NATS mock instance
        mock_nats_instance = AsyncMock()
        mock_nats_instance.close = AsyncMock()
        MockNATSClass.return_value = mock_nats_instance

        # Configure Redis mock instance
        mock_redis_instance = AsyncMock()
        mock_redis_instance.close = AsyncMock()
        mock_redis_from_url.return_value = mock_redis_instance

        # Configure the uvicorn mock
        mock_uvicorn_instance = MockUvicornServer.return_value
        mock_uvicorn_instance.serve = AsyncMock()
        mock_uvicorn_instance.shutdown = AsyncMock()

        # Configure the NATSListener mock instance
        mock_nats_listener_instance = MockNATSListener.return_value
        mock_nats_listener_instance.start = AsyncMock()
        mock_nats_listener_instance.stop = AsyncMock()
        
        mock_router_instance = MockOutputRouter.return_value
        mock_router_instance.close = AsyncMock()

        mock_builder_instance = MockContextBuilder.return_value
        mock_builder_instance.close = AsyncMock()

        mock_heartbeat_instance = MockHeartbeatResponder.return_value
        mock_heartbeat_instance.start = AsyncMock()
        mock_heartbeat_instance.stop = AsyncMock()

        with patch("asyncio.Event.wait", new_callable=AsyncMock):
            try:
                await main()
            except Exception as e:
                pytest.fail(f"main() failed unexpectedly with exception: {e}")

        # The core assertion: Verify that the listener was started with the correct wildcard subject
        mock_nats_listener_instance.start.assert_called_once_with(subject="cio.intent.trading.*")
