"""NATS alerts.> consumer — routes operator alerts to Telegram (petrosa_k8s#810 / AC4.1b).

Subscribes to the ``alerts.>`` subject tree, which captures every alert
family published across the Petrosa ecosystem:

* ``alerts.tradeengine.persist_failed.<symbol>``
* ``alerts.credentials.expiring.<id>``
* ``alerts.evaluator.unhealthy.<subsystem>``
* ``alerts.cio.<action>.<strategy_id>``
* ``alerts.drawdown.breach.<strategy_id>``

Each matching message is decoded as JSON, formatted as a human-readable
Telegram notification, and forwarded best-effort via
:class:`cio.core.alerting.telegram_channel.TelegramChannel`. Failures
are logged and counted but never raised — the NATS subscriber must not
crash due to a Telegram hiccup.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from prometheus_client import Counter

from cio.core.alerting.telegram_channel import TelegramChannel

if TYPE_CHECKING:
    from nats.aio.client import Client as NATS

logger = logging.getLogger(__name__)

ALERTS_SUBJECT_PATTERN = "alerts.>"

_received = Counter(
    "cio_alerts_consumer_received_total",
    "Total alerts.> messages received by the CIO alerts consumer",
    ["subject_prefix"],
)
_forwarded = Counter(
    "cio_alerts_consumer_forwarded_total",
    "Total alerts.> messages forwarded (or attempted) to Telegram",
    ["result"],
)


def _subject_prefix(subject: str) -> str:
    """First two tokens of subject for Prometheus label cardinality control."""
    parts = subject.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else subject


def _format_telegram_message(subject: str, payload: dict) -> str:
    severity = payload.get("severity", "unknown").upper()
    category = payload.get("category", subject)
    message = payload.get("message", "(no message)")
    timestamp = payload.get("timestamp", "")
    ts_line = f"\n<i>{timestamp}</i>" if timestamp else ""
    return (
        f"<b>[PETROSA ALERT] {severity}</b>\n"
        f"<b>Subject:</b> <code>{subject}</code>\n"
        f"<b>Category:</b> {category}\n"
        f"{message}{ts_line}"
    )


class AlertsConsumer:
    """NATS subscriber on ``alerts.>`` that forwards to Telegram.

    Follows the same lifecycle contract as
    :class:`cio.core.evaluator_subscriber.EvaluatorSubscriber`:
    ``start()`` creates the NATS subscription, ``stop()`` unsubscribes
    cleanly. Both are idempotent on repeated calls.
    """

    def __init__(
        self,
        nats_client: NATS,
        telegram: TelegramChannel | None = None,
    ) -> None:
        self._nc = nats_client
        self._telegram = telegram if telegram is not None else TelegramChannel()
        self._subscription = None

    async def start(self) -> None:
        self._subscription = await self._nc.subscribe(
            ALERTS_SUBJECT_PATTERN,
            cb=self._handle_message,
        )
        if not self._telegram.is_configured:
            logger.warning(
                "alerts_consumer: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — "
                "alerts.> messages will be received but NOT forwarded to Telegram"
            )
        logger.info(
            "alerts_consumer_started subject=%s telegram_configured=%s",
            ALERTS_SUBJECT_PATTERN,
            self._telegram.is_configured,
        )

    async def stop(self) -> None:
        if self._subscription is not None:
            try:
                await self._subscription.unsubscribe()
            except Exception as exc:  # noqa: BLE001
                logger.warning("alerts_consumer.unsubscribe_failed exc=%s", exc)
            self._subscription = None

    async def _handle_message(self, msg) -> None:
        subject = msg.subject
        _received.labels(subject_prefix=_subject_prefix(subject)).inc()

        try:
            payload = json.loads(msg.data.decode())
        except (json.JSONDecodeError, AttributeError) as exc:
            logger.warning(
                "alerts_consumer.parse_error subject=%s error=%s", subject, exc
            )
            _forwarded.labels(result="parse_error").inc()
            return

        text = _format_telegram_message(subject, payload)
        try:
            ok = await self._telegram.send(text)
        except Exception as exc:  # noqa: BLE001 — never crash the subscription loop
            logger.warning(
                "alerts_consumer.telegram_raised subject=%s exc=%s", subject, exc
            )
            ok = False
        result = "ok" if ok else "failed"
        _forwarded.labels(result=result).inc()
        if ok:
            logger.info("alerts_consumer.forwarded subject=%s", subject)
        else:
            logger.warning("alerts_consumer.forward_failed subject=%s", subject)
