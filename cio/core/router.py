import asyncio
import json
import logging
import os
from typing import Protocol

from cio.core.vector import VectorClientProtocol
from cio.models import ActionType, DecisionResult, TriggerContext
from cio.output.translator import TradeEngineTranslator

logger = logging.getLogger(__name__)


class NATSClientProtocol(Protocol):
    """Structural protocol for NATS client to ensure testability."""

    async def publish(self, subject: str, payload: bytes) -> None: ...


class OutputRouter:
    """
    Dispatches final DecisionResults to the Petrosa ecosystem via NATS.
    Implements the 'T-Junction' split for legacy and modern path alignment.
    """

    def __init__(
        self, nats_client: NATSClientProtocol, vector_client: VectorClientProtocol
    ):
        self.nats_client = nats_client
        self.vector_client = vector_client

    async def route(self, context: TriggerContext, decision: DecisionResult) -> None:
        """
        Routes the decision following the T-Junction logic:
        1. Legacy Path -> Translated Signal -> signals.trading (Only for EXECUTE)
        2. Modern Path -> Raw DecisionResult -> trade.execute.{id} (Only for EXECUTE)
        3. Audit Path -> DecisionResult + Context -> Vector DB (Always enabled for all actions)
        """
        correlation_id = context.correlation_id
        strategy_id = context.strategy_id
        action = decision.action or ActionType.SKIP

        # 0. Record Metrics
        from cio.core.metrics import DECISION_ACTIONS

        DECISION_ACTIONS.labels(action_type=action.value, strategy_id=strategy_id).inc()

        # 1. Prepare Audit Path (Memory storage - Always executed)
        audit_task = self.vector_client.upsert(
            strategy_id=strategy_id,
            payload={
                "event_type": "decision",
                "action": action.value,
                "correlation_id": correlation_id,
                "summary": decision.justification,
                "thought_trace": decision.thought_trace,
                "decision_data": decision.model_dump(),
            },
        )

        # 2. Prepare NATS Dispatch Path (T-Junction)
        dispatch_tasks_data: list[tuple[str, bytes]] = []

        if action == ActionType.EXECUTE:
            # LEGACY BRANCH: Translate to Signal model and send to legacy topic
            legacy_subject = os.getenv("NATS_TOPIC_SIGNALS", "signals.trading")
            legacy_data = TradeEngineTranslator.to_legacy_signal(context, decision)
            if legacy_data:
                dispatch_tasks_data.append(
                    (legacy_subject, json.dumps(legacy_data).encode())
                )

            # MODERN BRANCH: Send raw DecisionResult to vNext topic
            modern_subject = f"trade.execute.{strategy_id}"
            dispatch_tasks_data.append(
                (modern_subject, decision.model_dump_json().encode())
            )

        elif action == ActionType.MODIFY_PARAMS:
            dispatch_tasks_data.append(
                (
                    f"strategy.config.update.{strategy_id}",
                    decision.model_dump_json().encode(),
                )
            )
        elif action == ActionType.PAUSE_STRATEGY:
            dispatch_tasks_data.append(
                (
                    f"strategy.control.pause.{strategy_id}",
                    decision.model_dump_json().encode(),
                )
            )
        elif action == ActionType.ESCALATE:
            dispatch_tasks_data.append(
                (f"cio.escalation.{strategy_id}", decision.model_dump_json().encode())
            )

        # 3. Handle Dispatch execution (Checking DRY_RUN)
        is_dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
        nats_publish_tasks = []

        for subject, payload in dispatch_tasks_data:
            if is_dry_run:
                logger.info(
                    f"[SHADOW MODE] Would have published to {subject}",
                    extra={
                        "correlation_id": correlation_id,
                        "action": action.value,
                        "strategy_id": strategy_id,
                        "payload_preview": payload.decode()[:300],
                    },
                )
            else:
                nats_publish_tasks.append(self.nats_client.publish(subject, payload))

        # 4. Synchronize all operations (Audit + Publishes)
        # We use gather to fire both the audit write and the NATS publishes concurrently
        results = await asyncio.gather(
            audit_task, *nats_publish_tasks, return_exceptions=True
        )

        # Check for gathered errors
        for res in results:
            if isinstance(res, Exception):
                logger.error(
                    f"Background routing task failed: {res}",
                    extra={"correlation_id": correlation_id},
                )

        # 5. Audit log for no-publish actions
        if not dispatch_tasks_data and action in (ActionType.SKIP, ActionType.BLOCK):
            logger.info(
                "Action completed (no-publish required)",
                extra={
                    "correlation_id": correlation_id,
                    "action": action.value,
                    "strategy_id": strategy_id,
                    "subject": "no-publish",
                },
            )
        elif not is_dry_run and nats_publish_tasks:
            logger.info(
                "T-Junction dispatch successful",
                extra={
                    "correlation_id": correlation_id,
                    "action": action.value,
                    "strategy_id": strategy_id,
                    "targets": [t[0] for t in dispatch_tasks_data],
                },
            )
