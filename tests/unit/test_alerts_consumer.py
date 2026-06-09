"""Tests for AlertsConsumer and TelegramChannel (petrosa_k8s#810 / AC4.1b)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.alerting.telegram_channel import TelegramChannel
from cio.core.alerts_consumer import (
    AlertsConsumer,
    _format_telegram_message,
    _subject_prefix,
)

# ---------------------------------------------------------------------------
# TelegramChannel


class TestTelegramChannelConfigured:
    def test_is_configured_true_when_both_set(self):
        ch = TelegramChannel(bot_token="tok", chat_id="123")
        assert ch.is_configured is True

    def test_is_configured_false_when_token_missing(self):
        ch = TelegramChannel(bot_token="", chat_id="123")
        assert ch.is_configured is False

    def test_is_configured_false_when_chat_missing(self):
        ch = TelegramChannel(bot_token="tok", chat_id="")
        assert ch.is_configured is False


@pytest.mark.asyncio
async def test_telegram_send_returns_false_when_unconfigured():
    ch = TelegramChannel(bot_token="", chat_id="")
    result = await ch.send("hello")
    assert result is False


@pytest.mark.asyncio
async def test_telegram_send_returns_true_on_http_200():
    ch = TelegramChannel(bot_token="tok", chat_id="123")
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await ch.send("test message")

    assert result is True
    mock_client.post.assert_awaited_once()
    call_kwargs = mock_client.post.call_args
    assert "text" in call_kwargs.kwargs["json"]
    assert call_kwargs.kwargs["json"]["chat_id"] == "123"


@pytest.mark.asyncio
async def test_telegram_send_returns_false_on_non_200():
    ch = TelegramChannel(bot_token="tok", chat_id="123")
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await ch.send("test")

    assert result is False


@pytest.mark.asyncio
async def test_telegram_send_returns_false_on_network_error():
    ch = TelegramChannel(bot_token="tok", chat_id="123")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await ch.send("test")

    assert result is False


@pytest.mark.asyncio
async def test_telegram_reads_env_vars(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "envtok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "envid")
    ch = TelegramChannel()
    assert ch.is_configured is True
    assert ch._bot_token == "envtok"
    assert ch._chat_id == "envid"


# ---------------------------------------------------------------------------
# Helper functions


def test_subject_prefix_two_tokens():
    assert _subject_prefix("alerts.tradeengine") == "alerts.tradeengine"


def test_subject_prefix_longer_subject():
    assert (
        _subject_prefix("alerts.tradeengine.persist_failed.BTCUSDT")
        == "alerts.tradeengine"
    )


def test_subject_prefix_single_token():
    assert _subject_prefix("alerts") == "alerts"


def test_format_telegram_message_includes_subject_and_severity():
    payload = {
        "severity": "critical",
        "category": "tradeengine_persist_failed",
        "message": "Persist failed for BTCUSDT",
        "timestamp": "2026-06-09T10:00:00Z",
    }
    text = _format_telegram_message(
        "alerts.tradeengine.persist_failed.BTCUSDT", payload
    )
    assert "CRITICAL" in text
    assert "alerts.tradeengine.persist_failed.BTCUSDT" in text
    assert "Persist failed for BTCUSDT" in text
    assert "2026-06-09T10:00:00Z" in text


def test_format_telegram_message_no_timestamp():
    payload = {"severity": "warning", "category": "x", "message": "msg"}
    text = _format_telegram_message("alerts.x", payload)
    assert "<i>" not in text


# ---------------------------------------------------------------------------
# AlertsConsumer


def _make_nats_msg(subject: str, data: dict) -> MagicMock:
    msg = MagicMock()
    msg.subject = subject
    msg.data = json.dumps(data).encode()
    return msg


def _make_nats_client() -> AsyncMock:
    nc = AsyncMock()
    nc.subscribe = AsyncMock()
    return nc


@pytest.mark.asyncio
async def test_alerts_consumer_starts_and_subscribes():
    nc = _make_nats_client()
    telegram = MagicMock(spec=TelegramChannel)
    telegram.is_configured = True

    consumer = AlertsConsumer(nats_client=nc, telegram=telegram)
    await consumer.start()

    nc.subscribe.assert_awaited_once()
    call_args = nc.subscribe.call_args
    assert call_args.args[0] == "alerts.>"


@pytest.mark.asyncio
async def test_alerts_consumer_stop_unsubscribes():
    nc = _make_nats_client()
    mock_sub = AsyncMock()
    nc.subscribe = AsyncMock(return_value=mock_sub)
    telegram = MagicMock(spec=TelegramChannel)
    telegram.is_configured = True

    consumer = AlertsConsumer(nats_client=nc, telegram=telegram)
    await consumer.start()
    await consumer.stop()

    mock_sub.unsubscribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_alerts_consumer_stop_idempotent_when_not_started():
    nc = _make_nats_client()
    consumer = AlertsConsumer(nats_client=nc)
    await consumer.stop()  # must not raise


@pytest.mark.asyncio
async def test_alerts_consumer_forwards_valid_message():
    nc = _make_nats_client()
    telegram = AsyncMock(spec=TelegramChannel)
    telegram.is_configured = True
    telegram.send = AsyncMock(return_value=True)

    consumer = AlertsConsumer(nats_client=nc, telegram=telegram)
    msg = _make_nats_msg(
        "alerts.tradeengine.persist_failed.BTCUSDT",
        {
            "severity": "critical",
            "category": "tradeengine_persist_failed",
            "message": "fail",
        },
    )
    await consumer._handle_message(msg)

    telegram.send.assert_awaited_once()
    text = telegram.send.call_args.args[0]
    assert "CRITICAL" in text
    assert "alerts.tradeengine.persist_failed.BTCUSDT" in text


@pytest.mark.asyncio
async def test_alerts_consumer_drops_invalid_json_gracefully():
    nc = _make_nats_client()
    telegram = AsyncMock(spec=TelegramChannel)
    telegram.is_configured = True
    telegram.send = AsyncMock(return_value=True)

    consumer = AlertsConsumer(nats_client=nc, telegram=telegram)
    msg = MagicMock()
    msg.subject = "alerts.tradeengine.persist_failed.BTCUSDT"
    msg.data = b"not-json"

    await consumer._handle_message(msg)

    telegram.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_alerts_consumer_survives_telegram_failure():
    nc = _make_nats_client()
    telegram = AsyncMock(spec=TelegramChannel)
    telegram.is_configured = True
    telegram.send = AsyncMock(return_value=False)

    consumer = AlertsConsumer(nats_client=nc, telegram=telegram)
    msg = _make_nats_msg(
        "alerts.credentials.expiring.binance",
        {"severity": "warning", "message": "token expiring soon"},
    )
    await consumer._handle_message(msg)  # must not raise

    telegram.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_alerts_consumer_survives_telegram_exception():
    nc = _make_nats_client()
    telegram = AsyncMock(spec=TelegramChannel)
    telegram.is_configured = True
    telegram.send = AsyncMock(side_effect=Exception("network error"))

    consumer = AlertsConsumer(nats_client=nc, telegram=telegram)
    msg = _make_nats_msg("alerts.x.y", {"severity": "critical", "message": "boom"})

    # TelegramChannel.send catches exceptions internally and returns False,
    # but AlertsConsumer._handle_message must also be resilient.
    # The consumer delegates to telegram.send which here raises — ensure no crash.
    try:
        await consumer._handle_message(msg)
    except Exception:
        pytest.fail(
            "AlertsConsumer._handle_message must not propagate telegram exceptions"
        )
