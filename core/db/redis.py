"""Async Redis adapter for caching and policy distribution."""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis


class RedisPolicyAdapter:
    """Simple async Redis wrapper with pooling support."""

    def __init__(self, url: str):
        self.url = url
        self.client: Redis | None = None
        self.connected = False

    async def connect(self) -> None:
        self.client = Redis.from_url(self.url, decode_responses=True)
        await self.client.ping()
        self.connected = True

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.aclose()
        self.connected = False

    async def get_json(self, key: str) -> dict[str, Any] | None:
        if self.client is None:
            return None
        payload = await self.client.get(key)
        if not payload:
            return None
        return json.loads(payload)

    async def set_json(
        self, key: str, value: dict[str, Any], ex: int | None = None
    ) -> None:
        if self.client is None:
            return
        await self.client.set(key, json.dumps(value), ex=ex)

    async def delete(self, key: str) -> int:
        if self.client is None:
            return 0
        return await self.client.delete(key)
