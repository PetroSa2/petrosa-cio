"""Durable drawdown lifecycle metadata shared across CIO restarts."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from cio.core.durable_state import DataManagerStateStore

KEY = "drawdown_lifecycle"
FIELDS = ("halt_started_at", "resumed_drawdown", "resume_threshold")


class DrawdownLifecycleStore:
    def __init__(self, store: DataManagerStateStore) -> None:
        self.store = store
        self._save_lock = asyncio.Lock()

    async def load(self) -> dict[str, float | None] | None:
        raw = await self.store.get(KEY)
        if not raw:
            return None
        try:
            value = json.loads(raw)
            value = json.loads(value.get("gave_up_reason", ""))
        except (AttributeError, TypeError, ValueError):
            return None
        if not isinstance(value, dict) or any(key not in value for key in FIELDS):
            return None
        if any(
            value[key] is not None and not isinstance(value[key], int | float)
            for key in FIELDS
        ):
            return None
        return {key: value[key] for key in FIELDS}

    async def save(self, decision: Any) -> None:
        event = getattr(decision, "lifecycle_event", None)
        if event is None:
            event = {
                "drawdown_halt": "halt",
                "drawdown_resume": "resume",
                "drawdown_rehalt": "rehalt",
            }.get(getattr(decision, "reason", None))
        if event not in {"halt", "resume", "rehalt"}:
            return
        async with self._save_lock:
            record = {
                "halt_started_at": getattr(decision, "halt_started_at", None),
                "resumed_drawdown": getattr(decision, "resumed_drawdown", None),
                "resume_threshold": getattr(decision, "resume_threshold", None),
            }
            current = await self.load()
            current_started_at = current.get("halt_started_at") if current else None
            if (
                isinstance(current_started_at, int | float)
                and isinstance(record["halt_started_at"], int | float)
                and current_started_at > record["halt_started_at"]
            ):
                return
            payload = {
                "strategy_id": KEY,
                "service": "cio",
                "status": "drawdown_lifecycle",
                "paused_at": record["halt_started_at"] or 0.0,
                "last_unavailable_at": record["resumed_drawdown"] or 0.0,
                "min_pause_seconds": 0,
                "flap_count": 0,
                "attempts": 0,
                "next_attempt_at": record["resume_threshold"] or 0.0,
                "gave_up_reason": json.dumps(record, separators=(",", ":")),
            }
            await self.store.put(KEY, json.dumps(payload))
