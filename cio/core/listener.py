import json
import logging
import uuid
from typing import Protocol

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

from cio.core.arbiter import SignalArbiter
from cio.core.context_builder import ContextBuilder
from cio.core.router import OutputRouter
from cio.models import DecisionResult, TriggerContext, TriggerType

logger = logging.getLogger(__name__)


class EnforcerProtocol(Protocol):
    """Protocol for the NurseEnforcer."""

    async def audit(self, context: TriggerContext) -> DecisionResult: ...


class NATSListener:
    """
    NATS Subscriber for the Petrosa CIO.
    Listens for trade intents and triggers the reasoning loop.
    """

    def __init__(
        self,
        nats_client: NATS,
        enforcer: EnforcerProtocol,
        context_builder: ContextBuilder,
        router: OutputRouter,
        arbiter: SignalArbiter | None = None,
    ):
        self.nc = nats_client
        self.enforcer = enforcer
        self.context_builder = context_builder
        self.router = router
        self.arbiter = arbiter
        self.subscription = None

    async def start(self, subject: str = "trade.intent.*"):
        """Starts the NATS subscription."""
        self.subscription = await self.nc.subscribe(subject, cb=self._handle_message)
        logger.info(f"NATS Listener started on subject: {subject}")

    async def stop(self):
        """Drains and stops the subscription."""
        if self.subscription:
            await self.subscription.unsubscribe()
            logger.info("NATS Listener subscription drained and stopped.")

    async def _handle_message(self, msg: Msg):
        """
        Core message handler.
        Extracts correlation_id and triggers the reasoning loop.
        """
        # 1. Extract Correlation ID from headers or generate new one
        correlation_id = msg.headers.get("correlation_id") if msg.headers else None
        if not correlation_id:
            correlation_id = uuid.uuid4().hex
            logger.debug(f"No correlation_id in headers. Generated: {correlation_id}")

        # 2. Parse Payload
        try:
            payload_raw = msg.data.decode()
            payload = json.loads(payload_raw)
            logger.info(
                f"Received NATS payload on {msg.subject}: {payload_raw}",
                extra={
                    "correlation_id": correlation_id,
                    "symbol": payload.get("symbol"),
                    "action": payload.get("action"),
                },
            )
        except Exception as e:
            logger.error(
                f"Failed to parse NATS payload: {e}",
                extra={"correlation_id": correlation_id},
            )
            return

        # 3. Signal Arbitration (dedup + conflict resolution)
        if self.arbiter:
            symbol = payload.get("symbol", "")
            action = (
                payload.get("side")
                or payload.get("action")
                or payload.get("signal_type")
                or ""
            )
            confidence = float(payload.get("confidence", 0.5))
            strategy_id_raw = payload.get("strategy_id") or payload.get(
                "strategy", "unknown"
            )
            allowed, arb_reason = await self.arbiter.check(
                symbol=symbol,
                action=action,
                confidence=confidence,
                strategy_id=strategy_id_raw,
                correlation_id=correlation_id,
            )
            if not allowed:
                logger.info(
                    f"ARBITER_SUPPRESSED: {arb_reason}",
                    extra={"correlation_id": correlation_id},
                )
                return

        # 4. Assemble Context
        try:
            # For trade.intent.*, we assume TriggerType.TRADE_INTENT
            context = await self.context_builder.build(
                correlation_id=correlation_id,
                source_subject=msg.subject,
                trigger_type=TriggerType.TRADE_INTENT,
                payload=payload,
            )
        except Exception as e:
            logger.error(
                f"Failed to build context: {e}",
                extra={"correlation_id": correlation_id},
            )
            return

        # 5. Run NurseEnforcer (with Timeout Guard)
        try:
            decision = await self.enforcer.audit(context)
        except Exception as e:
            logger.error(
                f"Enforcer critical failure: {e}",
                extra={"correlation_id": correlation_id},
            )
            return

        # 6. Route Output
        try:
            await self.router.route(context, decision)
        except Exception as e:
            logger.error(
                f"OutputRouter critical failure: {e}",
                extra={"correlation_id": correlation_id},
            )
