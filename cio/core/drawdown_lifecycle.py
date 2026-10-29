"""Durable drawdown lifecycle metadata shared across CIO restarts."""

from __future__ import annotations

import json
import logging
from typing import Any

from cio.core.cache import AsyncRedisCache

logger = logging.getLogger(__name__)

KEY = "cio:drawdown:lifecycle"
TTL_SECONDS = 7 * 86400
FIELDS = ("halt_started_at", "resumed_drawdown", "resume_threshold")


class DrawdownLifecycleStore:
    def __init__(self, cache: AsyncRedisCache) -> None:
        self.cache = cache

    async def load(self) -> dict[str, float | None] | None:
        raw = await self.cache.get(KEY)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("DRAWDOWN_LIFECYCLE_INVALID_RECORD")
            return None
        if not isinstance(value, dict) or any(key not in value for key in FIELDS):
            logger.warning("DRAWDOWN_LIFECYCLE_INVALID_RECORD")
            return None
        if any(
            value[key] is not None and not isinstance(value[key], int | float)
            for key in FIELDS
        ):
            logger.warning("DRAWDOWN_LIFECYCLE_INVALID_RECORD")
            return None
        return {key: value[key] for key in FIELDS}

    async def save(self, decision: Any) -> None:
        event = getattr(decision, "lifecycle_event", None)
        if event not in {"halt", "resume", "rehalt"}:
            return
        record = {
            "halt_started_at": getattr(decision, "halt_started_at", None),
            "resumed_drawdown": getattr(decision, "resumed_drawdown", None),
            "resume_threshold": getattr(decision, "resume_threshold", None),
        }
        await self.cache.set(KEY, json.dumps(record), ttl=TTL_SECONDS)
