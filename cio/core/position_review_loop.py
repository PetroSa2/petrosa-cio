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
import os
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from opentelemetry.metrics import CallbackOptions, Observation
from prometheus_client import Counter, Gauge

from cio.core.metrics import meter

logger = logging.getLogger(__name__)


# Default cadence per FR60 decision_window. Overridden in cio/main.py via
# the ``CIO_REEVAL_INTERVAL_SECONDS`` env var.
DEFAULT_REEVAL_INTERVAL_SECONDS = 300.0


# The registry is bounded (petrosa-cio review-loop leak fix): positions are registered only for a real EXECUTE
# (see ``OutputRouter``) and retired by the close event; this cap is the last line of defence.
DEFAULT_MAX_POSITIONS = 200
# A registration that has seen no fill event for this long is retired: the EXECUTE was dispatched but the order was
# rejected, cancelled or lost before any fill, so no close event will ever retire it (labelled fallback, seconds).
DEFAULT_UNFILLED_TTL_SECONDS = 900.0


def unfilled_ttl_from_env() -> float:
    """``CIO_REVIEW_LOOP_UNFILLED_TTL_SECONDS`` (default 900 s); an unreadable or non-positive value is the default."""
    try:
        value = float(os.environ["CIO_REVIEW_LOOP_UNFILLED_TTL_SECONDS"])
    except (KeyError, ValueError):
        return DEFAULT_UNFILLED_TTL_SECONDS
    return value if value > 0 else DEFAULT_UNFILLED_TTL_SECONDS


def max_positions_from_env() -> int:
    """``CIO_REVIEW_LOOP_MAX_POSITIONS`` (default 200); an unreadable or non-positive value is the default."""
    try:
        value = int(os.environ["CIO_REVIEW_LOOP_MAX_POSITIONS"])
    except (KeyError, ValueError):
        return DEFAULT_MAX_POSITIONS
    return value if value > 0 else DEFAULT_MAX_POSITIONS


cio_review_loop_positions = Gauge(
    "cio_review_loop_positions",
    "Positions currently registered with the in-position review loop",
)

cio_review_loop_retired_unfilled = Counter(
    "cio_review_loop_retired_unfilled_total",
    "Registrations retired without ever seeing a fill (reason: ttl, or rejected by the trade engine)",
    ["reason"],
)

cio_review_loop_evicted = Counter(
    "cio_review_loop_evicted_total",
    "Positions evicted from the review loop because the registry was full (oldest first)",
)

_LOOPS: weakref.WeakSet[PositionReviewLoop] = weakref.WeakSet()


def _observe_positions(_options: CallbackOptions) -> Iterable[Observation]:
    yield Observation(sum(len(loop._positions) for loop in list(_LOOPS)))


# The CIO exports metrics over OTLP only, so the gauge is also an OTel observable gauge of the same name.
meter.create_observable_gauge(
    "cio_review_loop_positions",
    callbacks=[_observe_positions],
    description="Positions currently registered with the in-position review loop",
)

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


@dataclass
class _Registration:
    """When a position was registered and whether a fill event was seen for it."""

    registered_at: float
    filled: bool = False


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
        max_positions: int | None = None,
        unfilled_ttl_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be > 0, got {interval_seconds!r}")
        self._runner = runner
        self._interval = interval_seconds
        self._max_positions = (
            max_positions
            if max_positions and max_positions > 0
            else max_positions_from_env()
        )
        self._unfilled_ttl = (
            unfilled_ttl_seconds
            if unfilled_ttl_seconds and unfilled_ttl_seconds > 0
            else unfilled_ttl_from_env()
        )
        self._clock = clock or time.monotonic
        # key -> registration (insertion order = oldest first)
        self._positions: dict[PositionKey, _Registration] = {}
        self._inflight: set[PositionKey] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        _LOOPS.add(self)
        self._sync_gauge()

    def _sync_gauge(self) -> None:
        cio_review_loop_positions.set(
            sum(len(loop._positions) for loop in list(_LOOPS))
        )

    # ----- registry -------------------------------------------------------

    def add_position(self, strategy_id: str, position_id: str) -> bool:
        """Register a position so the cadence tick fires re-evals on it. Idempotent: returns True only when it
        was not registered yet. When the registry is full the OLDEST registration is evicted (logged, counted)."""
        key = PositionKey(strategy_id=strategy_id, position_id=position_id)
        if key in self._positions:
            return False
        while len(self._positions) >= self._max_positions:
            # the oldest registration that never saw a fill goes first; a position with a fill is real
            oldest = next(
                (k for k, reg in self._positions.items() if not reg.filled),
                next(iter(self._positions)),
            )
            del self._positions[oldest]
            cio_review_loop_evicted.inc()
            logger.warning(
                "position_review_loop.evicted position=%s max_positions=%d",
                oldest,
                self._max_positions,
            )
        self._positions[key] = _Registration(registered_at=self._clock())
        self._sync_gauge()
        logger.debug("position_review_loop.added position=%s", key)
        return True

    def remove_position(self, strategy_id: str, position_id: str) -> bool:
        """Drop a position from the cadence cycle (close / liquidation / EXIT_NOW). Idempotent: removing an
        unknown or already removed position is a no-op that returns False."""
        key = PositionKey(strategy_id=strategy_id, position_id=position_id)
        removed = self._positions.pop(key, None) is not None
        # Don't touch _inflight: the in-flight re-eval should complete naturally; its result is still useful for
        # audit even if the position has since closed.
        if removed:
            self._sync_gauge()
            logger.debug("position_review_loop.removed position=%s", key)
        return removed

    def mark_filled(self, strategy_id: str, position_id: str) -> bool:
        """An opening fill was seen for this position: it is real and is no longer subject to the unfilled TTL."""
        reg = self._positions.get(
            PositionKey(strategy_id=strategy_id, position_id=position_id)
        )
        if reg is None or reg.filled:
            return False
        reg.filled = True
        return True

    def retire_unfilled(
        self, strategy_id: str, position_id: str, *, reason: str = "rejected"
    ) -> bool:
        """Retire a registration that has seen no fill (the trade engine rejected or failed the order). A position
        that has a fill is NOT retired here: a rejection can concern a later order of a live position."""
        key = PositionKey(strategy_id=strategy_id, position_id=position_id)
        reg = self._positions.get(key)
        if reg is None or reg.filled:
            return False
        del self._positions[key]
        cio_review_loop_retired_unfilled.labels(reason=reason).inc()
        logger.info(
            "position_review_loop.retired_unfilled position=%s reason=%s", key, reason
        )
        self._sync_gauge()
        return True

    def expire_unfilled(self) -> list[PositionKey]:
        """Retire every registration older than the unfilled TTL that never saw a fill."""
        now = self._clock()
        expired = [
            key
            for key, reg in self._positions.items()
            if not reg.filled and now - reg.registered_at >= self._unfilled_ttl
        ]
        for key in expired:
            self.retire_unfilled(key.strategy_id, key.position_id, reason="ttl")
        return expired

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
            self.expire_unfilled()
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
