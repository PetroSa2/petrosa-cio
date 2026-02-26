"""Heartbeat watchdog that emits critical alerts after stale intervals."""

from __future__ import annotations

import time
from typing import Any

from core.alerting.manager import AlertEvent, AlertManager, AlertSeverity


class HeartbeatWatcher:
    """Monitors heartbeat recency and triggers CRITICAL alerts on timeout."""

    def __init__(
        self,
        *,
        alert_manager: AlertManager,
        stale_after_seconds: int = 60,
        clock: Any = time.time,
    ):
        self.alert_manager = alert_manager
        self.stale_after_seconds = stale_after_seconds
        self.clock = clock
        self.last_heartbeat_ts: float | None = None
        self.last_critical_alert_ts: float | None = None

    def record_heartbeat(self) -> None:
        self.last_heartbeat_ts = float(self.clock())

    async def check_health(self) -> bool:
        now = float(self.clock())
        if self.last_heartbeat_ts is None:
            return True

        elapsed = now - self.last_heartbeat_ts
        if elapsed <= self.stale_after_seconds:
            return True

        if self.last_critical_alert_ts is None or (
            now - self.last_critical_alert_ts >= self.stale_after_seconds
        ):
            event = AlertEvent(
                source="heartbeat_watcher",
                category="system_health",
                message=(
                    "Heartbeat stale for more than "
                    f"{self.stale_after_seconds} seconds"
                ),
                severity=AlertSeverity.CRITICAL,
                context={"stale_seconds": int(elapsed)},
            )
            await self.alert_manager.dispatch(event)
            self.last_critical_alert_ts = now

        return False
