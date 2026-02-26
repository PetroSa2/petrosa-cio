"""Deterministic CIO heartbeat handler over NATS request-reply."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from opentelemetry.trace import Status, StatusCode
from redis.asyncio import Redis

from otel_init import get_tracer


class HeartbeatService:
    """Serves `cio.heartbeat` responses with dependency health and telemetry."""

    def __init__(
        self,
        *,
        version: str,
        redis_url: str | None = None,
        mongo_url: str | None = None,
        response_budget_ms: float = 20.0,
    ):
        self.version = version
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        self.mongo_url = mongo_url or os.getenv("MONGO_URL")
        self.response_budget_ms = response_budget_ms
        self.tracer = get_tracer(__name__)

    async def check_redis_health(self) -> bool:
        if not self.redis_url:
            return False

        client = Redis.from_url(self.redis_url)
        try:
            return bool(await client.ping())
        except Exception:
            return False
        finally:
            await client.aclose()

    async def check_mongo_health(self) -> bool:
        if not self.mongo_url:
            return False

        client = AsyncIOMotorClient(self.mongo_url, serverSelectionTimeoutMS=500)
        try:
            response = await client.admin.command("ping")
            return float(response.get("ok", 0)) == 1.0
        except Exception:
            return False
        finally:
            client.close()

    async def build_heartbeat(self) -> dict[str, Any]:
        start = time.perf_counter()

        with self.tracer.start_as_current_span("cio.heartbeat") as span:
            redis_ok, mongo_ok = (
                await self.check_redis_health(),
                await self.check_mongo_health(),
            )
            elapsed_ms = (time.perf_counter() - start) * 1000.0

            status_code = "GOVERNANCE_ACTIVE" if redis_ok and mongo_ok else "DEGRADED"
            payload = {
                "status": "OK",
                "status_code": status_code,
                "timestamp": datetime.now(UTC).isoformat(),
                "version": self.version,
                "dependencies": {
                    "redis": "connected" if redis_ok else "disconnected",
                    "mongo": "connected" if mongo_ok else "disconnected",
                },
                "response_time_ms": elapsed_ms,
            }

            span.set_attribute("service.health.status", payload["status"])
            span.set_attribute("service.health.status_code", status_code)
            span.set_attribute("service.health.redis", redis_ok)
            span.set_attribute("service.health.mongo", mongo_ok)
            span.set_attribute("service.health.response_time_ms", elapsed_ms)
            span.set_attribute(
                "service.health.under_budget", elapsed_ms < self.response_budget_ms
            )

            if elapsed_ms >= self.response_budget_ms:
                span.set_status(
                    Status(StatusCode.ERROR, "heartbeat_response_over_budget")
                )

            return payload

    async def handle_request(self, msg: Any) -> None:
        payload = await self.build_heartbeat()
        await msg.respond(json.dumps(payload, separators=(",", ":")).encode())

    async def start(self, nats_client: Any) -> None:
        await nats_client.subscribe("cio.heartbeat", cb=self.handle_request)
