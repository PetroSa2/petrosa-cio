import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cio.core.heartbeat import HeartbeatResponder, HeartbeatPublisher

@pytest.mark.asyncio
async def test_heartbeat_publisher_lifecycle():
    """Test that HeartbeatPublisher starts, publishes, and stops cleanly."""
    mock_nc = AsyncMock()
    # Use short interval for testing
    publisher = HeartbeatPublisher(mock_nc, interval_seconds=0.01)
    
    await publisher.start("test.heartbeat")
    assert publisher.running is True
    assert publisher.task is not None
    
    # Wait for at least one publish
    await asyncio.sleep(0.05)
    
    mock_nc.publish.assert_called()
    args, _ = mock_nc.publish.call_args
    assert args[0] == "test.heartbeat"
    
    data = json.loads(args[1].decode())
    assert data["service"] == "petrosa-cio"
    assert data["status"] == "GOVERNANCE_ACTIVE"
    
    await publisher.stop()
    assert publisher.running is False
    assert publisher.task is None

@pytest.mark.asyncio
async def test_heartbeat_responder_handle_ping():
    """Test that HeartbeatResponder replies to a ping."""
    mock_nc = AsyncMock()
    responder = HeartbeatResponder(mock_nc)
    
    mock_msg = MagicMock()
    mock_msg.reply = "reply_subject"
    
    await responder._handle_ping(mock_msg)
    
    mock_nc.publish.assert_called_once()
    args, kwargs = mock_nc.publish.call_args
    assert args[0] == "reply_subject"
    
    response = json.loads(args[1].decode())
    assert response["status"] == "GOVERNANCE_ACTIVE"
    assert "health" in response
    assert response["health"]["redis"] is True
    assert response["health"]["mongodb"] is True

@pytest.mark.asyncio
async def test_heartbeat_responder_start_stop():
    """Test start and stop methods with realistic types."""
    mock_nc = AsyncMock()
    responder = HeartbeatResponder(mock_nc)
    
    # In nats-py, subscribe might return a Subscription object OR an sid (int)
    # We test the object path first
    mock_sub = AsyncMock()
    mock_nc.subscribe.return_value = mock_sub
    
    await responder.start("test.subject")
    mock_nc.subscribe.assert_called_once_with("test.subject", cb=responder._handle_ping)
    assert responder.subscription == mock_sub
    
    await responder.stop()
    mock_sub.unsubscribe.assert_called_once()
    assert responder.subscription is None

@pytest.mark.asyncio
async def test_heartbeat_responder_stop_with_sid():
    """Test stop method with an integer sid (nats-py behavior)."""
    mock_nc = AsyncMock()
    responder = HeartbeatResponder(mock_nc)
    
    responder.subscription = 123  # Mock sid
    
    await responder.stop()
    mock_nc.unsubscribe.assert_called_once_with(123)
    assert responder.subscription is None
