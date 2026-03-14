import asyncio
import logging
from typing import Any

from cio.core.alerting.channels import AlertChannel, EmailChannel, GrafanaChannel, OtelChannel

logger = logging.getLogger(__name__)

class RedundantAlertDispatcher:
    """
    Ensures that critical alerts are sent to ALL configured channels simultaneously.
    Provides fail-safe redundancy for high-severity failures.
    """
    def __init__(self):
        self.channels: list[AlertChannel] = [
            GrafanaChannel(),
            OtelChannel(),
            EmailChannel(),
        ]

    async def dispatch(self, message: str, context: dict[str, Any] | None = None) -> bool:
        """
        Dispatches an alert to all channels in parallel.
        Returns True if at least one channel succeeded.
        """
        ctx = context or {}
        tasks = [channel.send(message, ctx) for channel in self.channels]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        success_count = sum(1 for r in results if isinstance(r, bool) and r)
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Error in channel {self.channels[i].__class__.__name__}: {result}")
            elif not result:
                logger.warning(f"Channel {self.channels[i].__class__.__name__} failed to send alert.")

        return success_count > 0
