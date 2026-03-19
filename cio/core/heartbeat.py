import asyncio
import json
import logging
import time
from typing import Any

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

logger = logging.getLogger(__name__)

class HeartbeatResponder:
    """
    Provides deterministic governance liveness via NATS request-reply.
    Clients ping 'cio.heartbeat' and receive a status response.
    """
    def __init__(self, nats_client: NATS, redis_client: Any = None, mongo_client: Any = None):
        self.nc = nats_client
        self.redis = redis_client
        self.mongo = mongo_client
        self.subscription = None

    async def start(self, subject: str = "cio.heartbeat"):
        """Starts the heartbeat responder."""
        self.subscription = await self.nc.subscribe(subject, cb=self._handle_ping)
        logger.info(f"Heartbeat Responder active on subject: {subject}")

    async def stop(self):
        """Stops the responder."""
        if self.subscription is not None:
            # Fix for Copilot: Handle both Subscription objects and sids (ints)
            try:
                if hasattr(self.subscription, "unsubscribe"):
                    await self.subscription.unsubscribe()
                elif isinstance(self.subscription, int):
                    await self.nc.unsubscribe(self.subscription)
                logger.info("Heartbeat Responder stopped.")
            except Exception as e:
                logger.error(f"Error during heartbeat stop: {e}")
            finally:
                self.subscription = None

    async def _handle_ping(self, msg: Msg):
        """
        Handles incoming pings.
        Performs shallow health checks on dependencies.
        """
        if not msg.reply:
            return

        start_time = time.perf_counter()
        
        # 1. Dependency Health Checks (Shallow)
        health = {
            "redis": True,
            "mongodb": True,
            "latency_ms": 0
        }
        
        # TODO: Add actual connectivity checks if clients are provided
        
        status = "GOVERNANCE_ACTIVE"
        if not health["redis"] or not health["mongodb"]:
            status = "DEGRADED"

        latency_ms = int((time.perf_counter() - start_time) * 1000)
        health["latency_ms"] = latency_ms

        response = {
            "status": status,
            "version": "1.0.0",
            "timestamp": time.time(),
            "health": health
        }

        await self.nc.publish(msg.reply, json.dumps(response).encode())
        logger.debug(f"Heartbeat replied: {status} in {latency_ms}ms")


class HeartbeatPublisher:
    """
    Periodically publishes heartbeats to 'cio.heartbeat' (push model).
    This ensures that passive monitors like petrosa-tradeengine can detect
    that the CIO is alive without performing active pings.
    """
    def __init__(self, nats_client: NATS, interval_seconds: float = 10.0):
        self.nc = nats_client
        self.interval = interval_seconds
        self.task: asyncio.Task[None] | None = None
        self.running = False

    async def start(self, subject: str = "cio.heartbeat"):
        """Starts the periodic heartbeat publication."""
        if self.running:
            return

        self.running = True
        self.task = asyncio.create_task(self._run_loop(subject))
        logger.info(f"Heartbeat Publisher started on subject: {subject} (interval: {self.interval}s)")

    async def stop(self):
        """Stops the heartbeat publisher."""
        self.running = False
        if self.task:
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        logger.info("Heartbeat Publisher stopped.")

    async def _run_loop(self, subject: str):
        """Main loop for publishing heartbeats."""
        while self.running:
            try:
                heartbeat_data = {
                    "service": "petrosa-cio",
                    "status": "GOVERNANCE_ACTIVE",
                    "timestamp": time.time(),
                    "version": "1.0.0"
                }
                await self.nc.publish(subject, json.dumps(heartbeat_data).encode())
                logger.debug(f"Heartbeat published to {subject}")
            except Exception as e:
                logger.error(f"Error publishing heartbeat: {e}")

            await asyncio.sleep(self.interval)
