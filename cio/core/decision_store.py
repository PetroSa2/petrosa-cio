"""In-memory ring buffer of recent CIO decisions for dashboard consumption.

Populated by :class:`~cio.core.router.OutputRouter` at dispatch time.
Read by ``GET /api/dashboard/decisions/recent``.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cio.models.context import PreDecisionContext

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
    # FR53 / P3.4 (#130) — structured refusal source. None on accepts and on
    # refusals that are not categorized (legacy / "OTHER"); set to e.g.
    # "stale_characterization" by the CIO refusal gate so the operator
    # dashboard can group + filter without grepping `reasoning_trace`.
    rejection_source: str | None = None
    # FR53 / P3.4 (#130) — content-addressable revision the intent claimed.
    # Surfaced so the operator can compare against what the most-recent
    # characterization is bound to (i.e. spot drift at a glance).
    strategy_revision_id: str | None = None
    # P1.4-AC4 (#132) — structured PreDecisionContext snapshot at the
    # moment of dispatch. ``None`` on pre-EPIC-#122 historical records
    # (legacy ring-buffer entries written before this field existed) so
    # the dashboard surface can render "context not recorded" deterministically.
    pre_decision_context: PreDecisionContext | None = None
    # P1.5-AC3 (#137) — leverage that came out of the admission-time
    # arbiter. ``None`` on pre-EPIC-#691 historical records and on
    # legacy code paths that bypass the arbiter (defensive default so
    # adding the field does not break callers).
    decided_leverage: int | None = None


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
