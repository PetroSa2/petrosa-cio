"""In-position re-evaluation loop (P1.4-AC7, #135 / FR60).

Periodically fires a ``SCHEDULED_REVIEW`` trigger per active position so
the CIO arbitration loop reasons about an *open* position with fresh
market / portfolio / evaluator state, not just at admission time.

Two trigger sources land in this loop:

- **Cadence** (AC7.a) — every ``CIO_REEVAL_INTERVAL_SECONDS`` seconds
  (default 300, matching FR60's decision_window), every active position
  gets one ``SCHEDULED_REVIEW`` fired against it.
- **Event** (AC7.b) — callers fire :meth:`trigger_event` when something
  material happened (unhealthy evaluator verdict, regime shift, drawdown
  breach, characterization drift) so the loop re-evaluates *now*, not on
  the next cadence tick.

Backpressure (AC7.c): each ``(strategy_id, position_id)`` has at most
one re-evaluation in flight. A second trigger while the first is still
running is **dropped** and the ``cio_reeval_dropped_total`` Prometheus
counter is incremented. This prevents a slow arbitration path from
queueing an unbounded backlog when triggers arrive faster than they
complete (a real failure mode: regime shifts can cluster).

State is per-process and in-memory — same convention as
:class:`cio.core.evaluator_subscriber.EvaluatorSubscriber`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from prometheus_client import Counter

logger = logging.getLogger(__name__)


# Default cadence per FR60 decision_window. Overridden in cio/main.py via
# the ``CIO_REEVAL_INTERVAL_SECONDS`` env var.
DEFAULT_REEVAL_INTERVAL_SECONDS = 300.0


cio_reeval_fired = Counter(
    "cio_reeval_fired_total",
    "Re-evaluation trigger fires (cadence or event) per position",
    ["source"],  # cadence | event
)

cio_reeval_dropped = Counter(
    "cio_reeval_dropped_total",
    "Re-evaluation triggers dropped because a prior re-eval is still in flight",
    ["source"],
)


@dataclass(frozen=True)
class PositionKey:
    """Identity of an open position for backpressure bookkeeping."""

    strategy_id: str
    position_id: str

    def __str__(self) -> str:  # pragma: no cover — debug only
        return f"{self.strategy_id}:{self.position_id}"


# Source labels for the Prometheus counter and audit logs.
SOURCE_CADENCE = "cadence"
SOURCE_EVENT = "event"


# Callback signature for the arbitration runner. The loop calls this with the
# position key + a `reason` string; the runner is responsible for invoking
# the SCHEDULED_REVIEW trigger end-to-end (Code Engine + personas + router).
# The signature is intentionally minimal so callers can wire any test or
# production runner.
RunnerFn = Callable[[PositionKey, str], Awaitable[Any]]


class PositionReviewLoop:
    """In-position re-evaluation orchestrator.

    Usage from ``cio/main.py``::

        loop = PositionReviewLoop(
            runner=signal_arbiter.run_scheduled_review,
            interval_seconds=settings.CIO_REEVAL_INTERVAL_SECONDS,
        )
        loop.add_position("momentum-v3", "POS-1234")
        await loop.start()
        # … on EXIT_NOW / liquidation / close:
        loop.remove_position("momentum-v3", "POS-1234")
        await loop.stop()

    From an event hook (e.g. ``EvaluatorSubscriber`` on verdict transition,
    or :class:`cio.core.alerting.drawdown_breach_emitter.DrawdownBreachEmitter`
    when a breach fires)::

        await loop.trigger_event(
            PositionKey("momentum-v3", "POS-1234"),
            reason="evaluator_unhealthy:ingest",
        )
    """

    def __init__(
        self,
        runner: RunnerFn,
        *,
        interval_seconds: float = DEFAULT_REEVAL_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be > 0, got {interval_seconds!r}")
        self._runner = runner
        self._interval = interval_seconds
        self._positions: set[PositionKey] = set()
        self._inflight: set[PositionKey] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    # ----- registry -------------------------------------------------------

    def add_position(self, strategy_id: str, position_id: str) -> None:
        """Register a position so the cadence tick fires re-evals on it."""
        key = PositionKey(strategy_id=strategy_id, position_id=position_id)
        self._positions.add(key)
        logger.debug("position_review_loop.added position=%s", key)

    def remove_position(self, strategy_id: str, position_id: str) -> None:
        """Drop a position from the cadence cycle (close / liquidation / EXIT_NOW)."""
        key = PositionKey(strategy_id=strategy_id, position_id=position_id)
        self._positions.discard(key)
        # Don't touch _inflight — the in-flight re-eval should complete
        # naturally; its result is still useful for audit even if the
        # position has since closed.
        logger.debug("position_review_loop.removed position=%s", key)

    def active_positions(self) -> list[PositionKey]:
        """Snapshot of currently-registered positions (sorted for stability)."""
        return sorted(self._positions, key=lambda k: (k.strategy_id, k.position_id))

    # ----- task lifecycle -------------------------------------------------

    async def start(self) -> None:
        """Start the cadence task. Idempotent — safe to call twice."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._cadence_loop())
        logger.info("position_review_loop.started interval=%.1fs", self._interval)

    async def stop(self) -> None:
        """Signal stop and wait for the cadence task to exit cleanly."""
        self._stop_event.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:  # pragma: no cover — defensive
                pass
            self._task = None
        logger.info("position_review_loop.stopped")

    # ----- trigger surfaces -----------------------------------------------

    async def trigger_event(self, key: PositionKey, *, reason: str) -> bool:
        """Fire a re-evaluation for ``key`` outside the cadence (AC7.b).

        Returns ``True`` if the runner was invoked; ``False`` if the
        trigger was dropped by backpressure (AC7.c).
        """
        return await self._fire_once(key, source=SOURCE_EVENT, reason=reason)

    # ----- internals ------------------------------------------------------

    async def _cadence_loop(self) -> None:
        """Wake every ``_interval`` seconds and fire one re-eval per active position."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval,
                )
                # If the wait returns without timing out, stop was signaled.
                return
            except TimeoutError:
                pass

            # Snapshot the active set so a concurrent add/remove during the
            # iteration doesn't surprise us.
            for key in list(self._positions):
                await self._fire_once(
                    key, source=SOURCE_CADENCE, reason="scheduled_review_cadence"
                )

    async def _fire_once(
        self,
        key: PositionKey,
        *,
        source: str,
        reason: str,
    ) -> bool:
        """Attempt to dispatch one re-evaluation. Honors backpressure."""
        if key in self._inflight:
            cio_reeval_dropped.labels(source=source).inc()
            logger.info(
                "position_review_loop.dropped source=%s position=%s reason=%s "
                "(re-eval still in flight)",
                source,
                key,
                reason,
            )
            return False

        self._inflight.add(key)
        cio_reeval_fired.labels(source=source).inc()
        logger.info(
            "position_review_loop.fired source=%s position=%s reason=%s",
            source,
            key,
            reason,
        )
        try:
            await self._runner(key, reason)
        except Exception as exc:  # noqa: BLE001 — never crash the loop
            logger.warning(
                "position_review_loop.runner_failed position=%s reason=%s error=%s",
                key,
                reason,
                exc,
            )
        finally:
            self._inflight.discard(key)
        return True

    def snapshot(self) -> dict:
        """Debug/diagnostics export for the /state endpoint."""
        return {
            "interval_seconds": self._interval,
            "active": [
                {"strategy_id": k.strategy_id, "position_id": k.position_id}
                for k in self.active_positions()
            ],
            "inflight": [
                {"strategy_id": k.strategy_id, "position_id": k.position_id}
                for k in sorted(
                    self._inflight, key=lambda k: (k.strategy_id, k.position_id)
                )
            ],
        }
