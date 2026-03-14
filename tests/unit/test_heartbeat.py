import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from cio.core.heartbeat import HeartbeatResponder

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
    """Test start and stop methods."""
    mock_nc = AsyncMock()
    responder = HeartbeatResponder(mock_nc)
    
    await responder.start("test.subject")
    mock_nc.subscribe.assert_called_once_with("test.subject", cb=responder._handle_ping)
    
    responder.subscription = AsyncMock()
    await responder.stop()
    responder.subscription.unsubscribe.assert_called_once()
