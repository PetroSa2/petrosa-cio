"""Drawdown-envelope breach emitter (P8-AC2c, #140).

Owns the AC2.c.3 dedup window: a breach for the same
``(strategy_id, position_id)`` re-fires only after that position
exits AND a new one opens. The emitter does not subscribe to NATS
itself — the live drawdown vs. envelope comparator (CIO portfolio
tracker, eventually tradeengine position monitor) calls
:meth:`check_and_emit` on every observation and :meth:`notify_position_closed`
when the position lifecycle ends.

State is per-process and in-memory: the dedup record refreshes from
producer calls and a restart loses the "already alerted" set
(NFR-R1's detection-time window applies — same convention as
:class:`cio.core.evaluator_subscriber.EvaluatorSubscriber`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from cio.core.alerting.fr66_alerts import (
    SEVERITY_CRITICAL,
    build_drawdown_breach_alert,
    drawdown_breach_subject,
    publish_fr66_alert,
)

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

if TYPE_CHECKING:
    from cio.core.alerting.fr66_alerts import _NATSPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BreachRecord:
    """Last breach observation for one ``(strategy_id, position_id)`` pair."""

    strategy_id: str
    position_id: str
    observed_drawdown: float
    envelope_p99: float
    fired_at: datetime


class DrawdownBreachEmitter:
    """Stateful emitter for ``alerts.drawdown.breach.<strategy_id>``.

    Usage from a producer (CIO portfolio tracker, etc.)::

        emitter = DrawdownBreachEmitter(nats_client=nc)
        # On each drawdown observation:
        await emitter.check_and_emit(
            strategy_id="momentum-v3",
            position_id="POS-1234",
            realized_drawdown_pct=0.0712,
            envelope_p99=0.06,
            envelope_p100=0.082,
        )
        # When the position closes:
        emitter.notify_position_closed("momentum-v3", "POS-1234")

    The emitter is intentionally NATS-agnostic — pass any object that
    satisfies :class:`cio.core.alerting.fr66_alerts._NATSPublisher` (the
    structural protocol used across the FR66 producers).
    """

    def __init__(self, nats_client: _NATSPublisher | None = None) -> None:
        self._nc = nats_client
        # (strategy_id, position_id) → last breach record. Presence
        # means "already alerted this position; suppress until close".
        self._fired: dict[tuple[str, str], BreachRecord] = {}

    async def check_and_emit(
        self,
        *,
        strategy_id: str,
        position_id: str,
        realized_drawdown_pct: float,
        envelope_p99: float,
        envelope_p100: float | None = None,
        severity: str = SEVERITY_CRITICAL,
        observed_at: datetime | None = None,
    ) -> bool:
        """Emit a breach alert if realized > p99 AND not already alerted.

        Returns ``True`` if an alert was emitted (or attempted — actual
        NATS success is best-effort, see ``publish_fr66_alert``).
        Returns ``False`` when no breach, or when the dedup window
        suppresses a repeat for the same ``(strategy_id, position_id)``.
        """
        if realized_drawdown_pct <= envelope_p99:
            return False

        key = (strategy_id, position_id)
        if key in self._fired:
            logger.debug(
                "drawdown_breach.dedupe_suppressed strategy_id=%s position_id=%s "
                "realized=%.4f envelope_p99=%.4f",
                strategy_id,
                position_id,
                realized_drawdown_pct,
                envelope_p99,
            )
            return False

        now = observed_at or datetime.now(UTC)
        payload = build_drawdown_breach_alert(
            strategy_id=strategy_id,
            position_id=position_id,
            observed_drawdown=realized_drawdown_pct,
            envelope_p99=envelope_p99,
            envelope_p100=envelope_p100,
            severity=severity,
            observed_at=now,
        )
        logger.info(
            "drawdown_breach.detected strategy_id=%s position_id=%s "
            "realized=%.4f envelope_p99=%.4f envelope_p100=%s",
            strategy_id,
            position_id,
            realized_drawdown_pct,
            envelope_p99,
            envelope_p100,
        )
        await publish_fr66_alert(
            self._nc,
            subject=drawdown_breach_subject(strategy_id),
            payload=payload,
        )
        self._fired[key] = BreachRecord(
            strategy_id=strategy_id,
            position_id=position_id,
            observed_drawdown=realized_drawdown_pct,
            envelope_p99=envelope_p99,
            fired_at=now,
        )
        return True

    def notify_position_closed(self, strategy_id: str, position_id: str) -> None:
        """Clear the dedup record for a ``(strategy_id, position_id)`` pair.

        Callers must fire this on every position exit (close, liquidation,
        manual EXIT_NOW). Without this signal AC2.c.3 cannot re-arm — a
        breach on the same identifier after a missed close would be
        silently suppressed. Missing/duplicate calls are tolerated (this
        is best-effort hygiene, not a contract).
        """
        key = (strategy_id, position_id)
        if self._fired.pop(key, None) is not None:
            logger.debug(
                "drawdown_breach.dedupe_cleared strategy_id=%s position_id=%s",
                strategy_id,
                position_id,
            )

    def fired_keys(self) -> list[tuple[str, str]]:
        """Snapshot of currently-suppressed ``(strategy_id, position_id)`` pairs."""
        return sorted(self._fired.keys())

    def snapshot(self) -> dict:
        """State export for debug/diagnostics endpoints."""
        return {
            "fired": [
                {
                    "strategy_id": r.strategy_id,
                    "position_id": r.position_id,
                    "observed_drawdown": r.observed_drawdown,
                    "envelope_p99": r.envelope_p99,
                    "fired_at": r.fired_at.isoformat(),
                }
                for r in self._fired.values()
            ],
        }
