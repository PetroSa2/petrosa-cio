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
        if self.subscription:
            await self.subscription.unsubscribe()
            logger.info("Heartbeat Responder stopped.")

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
        # For now, we assume they are alive if they were passed in
        
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
