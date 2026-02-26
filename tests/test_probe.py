"""
Unit tests for BinanceProbe.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from core.probe import BinanceProbe


@pytest_asyncio.fixture
async def probe_instance():
    async with BinanceProbe(base_url="https://test.binance.com") as p:
        yield p


@pytest.mark.asyncio
async def test_ping_success(probe_instance):
    with patch.object(probe_instance.client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(
            200,
            json={},
            request=httpx.Request("GET", "https://test.binance.com/fapi/v1/ping"),
        )
        result = await probe_instance.ping()
        assert result is True
        mock_get.assert_called_once_with("/fapi/v1/ping", params=None)


@pytest.mark.asyncio
async def test_ping_failure(probe_instance):
    with patch.object(probe_instance.client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.HTTPStatusError(
            "Error",
            request=httpx.Request("GET", "https://test.binance.com/fapi/v1/ping"),
            response=httpx.Response(500),
        )
        result = await probe_instance.ping()
        assert result is False


@pytest.mark.asyncio
async def test_get_server_time(probe_instance):
    with patch.object(probe_instance.client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(
            200,
            json={"serverTime": 123456789},
            request=httpx.Request("GET", "https://test.binance.com/fapi/v1/time"),
        )
        result = await probe_instance.get_server_time()
        assert result == 123456789


@pytest.mark.asyncio
async def test_get_price(probe_instance):
    with patch.object(probe_instance.client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(
            200,
            json={"symbol": "BTCUSDT", "price": "50000.00"},
            request=httpx.Request(
                "GET", "https://test.binance.com/fapi/v1/ticker/price"
            ),
        )
        result = await probe_instance.get_price("BTCUSDT")
        assert result == 50000.0
        mock_get.assert_called_once_with(
            "/fapi/v1/ticker/price", params={"symbol": "BTCUSDT"}
        )


@pytest.mark.asyncio
async def test_get_drift_metrics(probe_instance):
    with patch.object(
        probe_instance, "get_server_time", new_callable=AsyncMock
    ) as mock_time:
        with patch.object(
            probe_instance, "get_price", new_callable=AsyncMock
        ) as mock_price:
            mock_time.return_value = 1000000000000
            mock_price.return_value = 50000.0

            result = await probe_instance.get_drift_metrics("BTCUSDT")

            assert result["symbol"] == "BTCUSDT"
            assert result["binance_server_time"] == 1000000000000
            assert result["current_price"] == 50000.0
            assert "drift_ms" in result
            assert "probe_latency_ms" in result
            assert "status" in result
