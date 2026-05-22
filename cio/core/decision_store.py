"""In-memory ring buffer of recent CIO decisions for dashboard consumption.

Populated by :class:`~cio.core.router.OutputRouter` at dispatch time.
Read by ``GET /api/dashboard/decisions/recent``.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

_UTC = timezone.utc  # noqa: UP017
DEFAULT_MAX_SIZE = 500


@dataclass
class DecisionRecord:
    strategy_id: str
    action: str
    reasoning_trace: str
    confidence: float
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(_UTC))


class DecisionStore:
    def __init__(self, maxlen: int = DEFAULT_MAX_SIZE) -> None:
        self._records: deque[DecisionRecord] = deque(maxlen=maxlen)

    def record(self, entry: DecisionRecord) -> None:
        self._records.append(entry)

    def recent(
        self,
        since: datetime,
        strategy_id: str | None = None,
    ) -> list[DecisionRecord]:
        results = [r for r in self._records if r.timestamp >= since]
        if strategy_id:
            results = [r for r in results if r.strategy_id == strategy_id]
        return sorted(results, key=lambda r: r.timestamp, reverse=True)
