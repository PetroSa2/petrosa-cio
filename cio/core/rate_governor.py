"""
CIO Rate Governor Service.
Subscribes to Binance rate limit updates and provides throttling logic.
"""

import json
import logging
import time
from typing import Any, Optional

import nats
import nats.aio.client
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class RateLimitStatus(BaseModel):
    """Rate limit status model (mirrored from TradeEngine)."""
    weight_1m: int
    timestamp: float


class RateGovernor:
    """Governs outgoing requests based on ecosystem-wide rate limits."""

    def __init__(self, nats_url: str, subject: str = "exchange.binance.rate_limits"):
        self.nats_url = nats_url
        self.subject = subject
        self.current_weight: int = 0
        self.max_weight: int = 1200  # Binance default for 1m
        self.threshold_pct: float = 0.85
        self.nats_client: Optional[nats.aio.client.Client] = None
        self.is_running: bool = False

    async def start(self):
        """Start the governor and subscribe to rate limits."""
        try:
            self.nats_client = await nats.connect(self.nats_url)
            await self.nats_client.subscribe(self.subject, cb=self._message_handler)
            self.is_running = True
            logger.info(f"RateGovernor started, subscribed to {self.subject}")
        except Exception as e:
            logger.error(f"RateGovernor failed to start: {e}")
            self.is_running = False

    async def stop(self):
        """Stop the governor and close NATS connection."""
        if self.nats_client:
            await self.nats_client.close()
            self.nats_client = None
        self.is_running = False

    async def _message_handler(self, msg):
        """Handle incoming rate limit messages."""
        try:
            data = json.loads(msg.data.decode())
            status = RateLimitStatus(**data)
            self.current_weight = status.weight_1m
            logger.debug(f"RateGovernor updated weight: {self.current_weight}")
        except Exception as e:
            logger.error(f"RateGovernor failed to parse message: {e}")

    def is_throttled(self) -> bool:
        """Check if requests should be throttled."""
        return self.current_weight >= (self.max_weight * self.threshold_pct)

    def get_status(self) -> dict[str, Any]:
        """Get current rate limit status."""
        return {
            "current_weight": self.current_weight,
            "max_weight": self.max_weight,
            "threshold": int(self.max_weight * self.threshold_pct),
            "is_throttled": self.is_throttled(),
            "usage_pct": round((self.current_weight / self.max_weight) * 100, 2)
        }
