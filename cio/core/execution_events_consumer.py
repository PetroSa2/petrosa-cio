"""NATS ``execution.events.>`` consumer — closes the position lifecycle loop
back into CIO (petrosa_k8s#1130).

The trade engine publishes one message per order lifecycle event on
``execution.events.<strategy_id>`` (petrosa_k8s#586, P0.2c). Nothing in CIO
consumed that subject before this ticket, so ``PortfolioTracker.record_exit``
(a complete implementation, never driven) and
``PositionReviewLoop.remove_position`` were never called on a real close —
closed positions stayed in both the aggregate-leverage ledger and the
in-position re-evaluation cadence indefinitely (ghost positions, #1128/#1129).

A position is considered fully closed, and is dropped from both trackers,
when either:

* the payload's ``event_type`` is ``"filled"`` and ``position_status`` is
  ``"closed"`` (as opposed to ``"partial"`` — a ``scale_out`` reduce-only
  fill that leaves the strategy position open at a reduced size, per
  petrosa_k8s#1130's tradeengine-side ``handle_cio_position_lifecycle_action``
  and the pre-existing OCO SL/TP trigger path); or
* the payload's ``event_type`` is ``"position_force_closed_no_stops"`` (the
  naked-position watchdog force-close path, ``position_health_guard.py``) —
  this event type is unconditionally a full close, no ``position_status``
  required.

``client_order_id`` on the payload is the CIO-assigned synthetic
``position_id`` (petrosa_k8s#1127), echoed back unchanged by the trade
engine. It maps 1:1 onto the second half of
:class:`cio.core.position_review_loop.PositionKey`. Older/incomplete
payloads that lack it fall back to ``strategy_id`` as the position_id —
the same degrade-gracefully convention the admission side
(``Orchestrator.run``) already uses for ``context.position_id``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from prometheus_client import Counter

from cio.core.portfolio_tracker import PortfolioTracker
from cio.core.portfolio_tracker import portfolio_tracker as _default_portfolio_tracker

if TYPE_CHECKING:
    from nats.aio.client import Client as NATS

    from cio.core.position_review_loop import PositionReviewLoop

logger = logging.getLogger(__name__)

EXECUTION_EVENTS_SUBJECT_PATTERN = "execution.events.>"

_CLOSING_EVENT_TYPES_REQUIRING_STATUS = {"filled"}
_UNCONDITIONAL_CLOSING_EVENT_TYPES = {"position_force_closed_no_stops"}

_received = Counter(
    "cio_execution_events_consumer_received_total",
    "Total execution.events.> messages received by the CIO",
    ["event_type"],
)
_position_closed = Counter(
    "cio_execution_events_consumer_position_closed_total",
    "Total position closures processed (record_exit + remove_position fired)",
    ["event_type"],
)


def _is_position_closed(payload: dict) -> bool:
    event_type = payload.get("event_type")
    if event_type in _UNCONDITIONAL_CLOSING_EVENT_TYPES:
        return True
    if event_type in _CLOSING_EVENT_TYPES_REQUIRING_STATUS:
        return payload.get("position_status") == "closed"
    return False


class ExecutionEventsConsumer:
    """NATS subscriber on ``execution.events.>`` that retires closed positions.

    Follows the same lifecycle contract as
    :class:`cio.core.alerts_consumer.AlertsConsumer` /
    :class:`cio.core.evaluator_subscriber.EvaluatorSubscriber`: ``start()``
    creates the subscription, ``stop()`` unsubscribes cleanly.
    """

    def __init__(
        self,
        nats_client: NATS,
        portfolio_tracker: PortfolioTracker | None = None,
        position_review_loop: PositionReviewLoop | None = None,
    ) -> None:
        self._nc = nats_client
        self._portfolio_tracker = (
            portfolio_tracker
            if portfolio_tracker is not None
            else _default_portfolio_tracker
        )
        self._position_review_loop = position_review_loop
        self._subscription = None

    async def start(self) -> None:
        self._subscription = await self._nc.subscribe(
            EXECUTION_EVENTS_SUBJECT_PATTERN,
            cb=self._handle_message,
        )
        logger.info(
            "execution_events_consumer_started subject=%s position_review_loop=%s",
            EXECUTION_EVENTS_SUBJECT_PATTERN,
            "wired" if self._position_review_loop is not None else "disabled",
        )

    async def stop(self) -> None:
        if self._subscription is not None:
            try:
                await self._subscription.unsubscribe()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execution_events_consumer.unsubscribe_failed exc=%s", exc
                )
            self._subscription = None

    async def _handle_message(self, msg) -> None:
        subject = msg.subject

        try:
            payload = json.loads(msg.data.decode())
        except (json.JSONDecodeError, AttributeError) as exc:
            logger.warning(
                "execution_events_consumer.parse_error subject=%s error=%s",
                subject,
                exc,
            )
            return

        event_type = payload.get("event_type", "unknown")
        _received.labels(event_type=event_type).inc()

        if not _is_position_closed(payload):
            return

        strategy_id = payload.get("strategy_id")
        if not strategy_id or strategy_id == "unknown":
            logger.warning(
                "execution_events_consumer.missing_strategy_id subject=%s "
                "event_type=%s — cannot retire position",
                subject,
                event_type,
            )
            return

        # petrosa_k8s#1127: client_order_id IS the CIO-assigned position_id,
        # echoed unchanged by the trade engine. Fall back to strategy_id for
        # payloads that predate the round-trip (same convention as
        # `context.position_id or context.strategy_id` at admission).
        position_id = payload.get("client_order_id") or strategy_id

        await self._portfolio_tracker.record_exit(strategy_id=strategy_id)

        if self._position_review_loop is not None:
            self._position_review_loop.remove_position(strategy_id, position_id)

        _position_closed.labels(event_type=event_type).inc()
        logger.info(
            "execution_events_consumer.position_retired strategy_id=%s "
            "position_id=%s event_type=%s subject=%s",
            strategy_id,
            position_id,
            event_type,
            subject,
        )
