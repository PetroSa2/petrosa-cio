"""
Shadow Validation Probe for read-only connectivity to Binance.
Used for drift calibration and market reality verification.
"""

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from otel_init import get_meter, get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)
meter = get_meter(__name__)

# Metrics
probe_requests = meter.create_counter(
    "probe_requests_total", description="Total number of probe requests", unit="1"
)
probe_latency = meter.create_histogram(
    "probe_latency_ms", description="Probe request latency in milliseconds", unit="ms"
)


class BinanceProbe:
    """
    Read-only probe for Binance Futures API.
    Provides methods to fetch market state for drift calibration.
    """

    def __init__(
        self,
        base_url: str = "https://fapi.binance.com",
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "User-Agent": "Petrosa-CIO-Probe/1.0",
                "Content-Type": "application/json",
            },
        )
        logger.info(f"BinanceProbe initialized with base_url: {self.base_url}")

    async def close(self):
        """Close the underlying HTTP client."""
        await self.client.aclose()
        logger.info("BinanceProbe client closed")

    async def _get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> Any:
        """Internal GET helper with error handling and instrumentation."""
        probe_requests.add(1, {"endpoint": endpoint})
        start_time = time.time()

        with tracer.start_as_current_span(f"binance_probe_get_{endpoint}"):
            try:
                response = await self.client.get(endpoint, params=params)
                response.raise_for_status()
                data = response.json()

                latency = (time.time() - start_time) * 1000
                probe_latency.record(
                    latency, {"endpoint": endpoint, "status": "success"}
                )

                return data
            except httpx.HTTPStatusError as e:
                latency = (time.time() - start_time) * 1000
                probe_latency.record(latency, {"endpoint": endpoint, "status": "error"})
                logger.error(
                    f"HTTP error occurred: {e.response.status_code} - {e.response.text}"
                )
                raise
            except Exception as e:
                latency = (time.time() - start_time) * 1000
                probe_latency.record(
                    latency, {"endpoint": endpoint, "status": "exception"}
                )
                logger.error(f"An unexpected error occurred: {e}")
                raise

    async def ping(self) -> bool:
        """Check connectivity to Binance API."""
        try:
            await self._get("/fapi/v1/ping")
            return True
        except Exception:
            return False

    async def get_server_time(self) -> int:
        """Get Binance server time in milliseconds."""
        data = await self._get("/fapi/v1/time")
        return data["serverTime"]

    async def get_exchange_info(self) -> dict[str, Any]:
        """Get exchange information (symbols, limits, etc.)."""
        return await self._get("/fapi/v1/exchangeInfo")

    async def get_price(self, symbol: str) -> float:
        """Get latest price for a symbol."""
        data = await self._get(
            "/fapi/v1/ticker/price", params={"symbol": symbol.upper()}
        )
        return float(data["price"])

    async def get_ticker_24h(self, symbol: str) -> dict[str, Any]:
        """Get 24h ticker statistics."""
        return await self._get(
            "/fapi/v1/ticker/24hr", params={"symbol": symbol.upper()}
        )

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> list[list[Any]]:
        """
        Get kline/candlestick data.

        Args:
            symbol: Trading symbol (e.g., BTCUSDT)
            interval: Kline interval (e.g., 1m, 1h, 1d)
            limit: Number of klines to fetch (max 1500)
        """
        params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        return await self._get("/fapi/v1/klines", params=params)

    async def get_drift_metrics(self, symbol: str) -> dict[str, Any]:
        """
        Calculate basic drift metrics for a symbol.
        Compares local perception with Binance reality.
        """
        start_time_local = time.time()
        server_time = await self.get_server_time()
        price = await self.get_price(symbol)
        latency = (time.time() - start_time_local) * 1000

        local_ms = int(time.time() * 1000)
        drift_ms = local_ms - server_time

        return {
            "symbol": symbol.upper(),
            "binance_server_time": server_time,
            "local_timestamp": local_ms,
            "drift_ms": drift_ms,
            "current_price": price,
            "probe_latency_ms": latency,
            "status": "synchronized" if abs(drift_ms) < 5000 else "drift_detected",
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
