from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.alerting.channels import EmailChannel, GrafanaChannel, OtelChannel
from cio.core.alerting.manager import AlertManager
from cio.core.alerting.redundancy import RedundantAlertDispatcher


@pytest.mark.asyncio
async def test_alert_manager_dispatch():
    """Test that AlertManager calls the dispatcher."""
    with patch.object(
        AlertManager, "_dispatcher", new_callable=AsyncMock
    ) as mock_dispatcher:
        await AlertManager.dispatch_critical_alert("Test alert", {"key": "value"})
        mock_dispatcher.dispatch.assert_called_once_with(
            "Test alert",
            {"alert_type": "RED", "correlation_id": "SYSTEM", "key": "value"},
        )


@pytest.mark.asyncio
async def test_redundant_dispatcher_all_channels():
    """Test that RedundantAlertDispatcher calls all channels."""
    dispatcher = RedundantAlertDispatcher()

    # Mock all channels
    for channel in dispatcher.channels:
        channel.send = AsyncMock(return_value=True)

    success = await dispatcher.dispatch("Test message", {"foo": "bar"})

    assert success is True
    for channel in dispatcher.channels:
        channel.send.assert_called_once_with("Test message", {"foo": "bar"})


@pytest.mark.asyncio
async def test_redundant_dispatcher_partial_failure():
    """Test that RedundantAlertDispatcher succeeds if at least one channel works."""
    dispatcher = RedundantAlertDispatcher()

    dispatcher.channels[0].send = AsyncMock(return_value=False)
    dispatcher.channels[1].send = AsyncMock(side_effect=Exception("Otel failed"))
    dispatcher.channels[2].send = AsyncMock(return_value=True)

    success = await dispatcher.dispatch("Test message")
    assert success is True


@pytest.mark.asyncio
async def test_grafana_channel_http_call():
    """Test GrafanaChannel HTTP API call."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.status_code = 200

        with patch.dict(
            "os.environ",
            {"GRAFANA_API_URL": "http://grafana", "GRAFANA_API_KEY": "key"},
        ):
            channel = GrafanaChannel()
            await channel.send("Test Grafana", {"alert_type": "RED"})

            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert "annotations" in args[0]
            assert kwargs["json"]["text"] == "Test Grafana"


@pytest.mark.asyncio
async def test_otel_channel_span():
    """Test OtelChannel creates a span."""
    with patch("opentelemetry.trace.get_tracer") as mock_get_tracer:
        mock_tracer = MagicMock()
        mock_get_tracer.return_value = mock_tracer
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__.return_value = (
            mock_span
        )

        channel = OtelChannel()
        await channel.send("Test Otel", {"alert_type": "RED"})

        mock_tracer.start_as_current_span.assert_called_once_with("CRITICAL_ALERT")
        mock_span.set_attribute.assert_any_call("alert.message", "Test Otel")


@pytest.mark.asyncio
async def test_email_channel_smtp():
    """Test EmailChannel SMTP call."""
    with patch("smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        with patch.dict(
            "os.environ",
            {
                "SMTP_USER": "user",
                "SMTP_PASS": "pass",
                "SMTP_HOST": "host",
                "SMTP_PORT": "587",
            },
        ):
            channel = EmailChannel()
            await channel.send("Test Email", {"alert_type": "RED"})

            mock_server.starttls.assert_called_once()
            mock_server.login.assert_called_once_with("user", "pass")
            mock_server.send_message.assert_called_once()
