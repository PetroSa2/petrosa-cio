import asyncio
import json
import logging
import os
from typing import Protocol

import httpx

from cio.core.service_resolver import ServiceType, TargetServiceResolver
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
        self,
        nats_client: NATSClientProtocol,
        vector_client: VectorClientProtocol,
        ta_bot_url: str | None = None,
        realtime_strategies_url: str | None = None,
        cache=None,
    ):
        self.nats_client = nats_client
        self.vector_client = vector_client
        # Allow explicit arguments to override environment-based configuration.
        self.ta_bot_url = ta_bot_url or os.getenv("TA_BOT_URL", "")
        self.realtime_strategies_url = realtime_strategies_url or os.getenv(
            "REALTIME_STRATEGIES_URL", ""
        )
        self.cache = cache

        if not self.ta_bot_url:
            logger.warning(
                "CONFIG_WARNING: TA bot URL is not configured. "
                "HTTP calls for TA bot routing may fail."
            )

        if not self.realtime_strategies_url:
            logger.warning(
                "CONFIG_WARNING: Realtime strategies URL is not configured. "
                "HTTP calls for realtime strategies routing may fail."
            )

        token = os.getenv("PETROSA_INTERNAL_TOKEN", "")
        if not token:
            logger.warning(
                "SECURITY_WARNING: PETROSA_INTERNAL_TOKEN is not set. "
                "All internal HTTP requests from OutputRouter will be unauthenticated."
            )

        self.http_client = httpx.AsyncClient(
            headers={
                "X-Petrosa-Issuer": "CIO",
                "X-Petrosa-Internal-Token": token,
            },
            timeout=httpx.Timeout(15.0, connect=15.0, read=15.0, write=15.0),
        )

    async def close(self) -> None:
        """Closes internal resources."""
        await self.http_client.aclose()

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
        is_dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

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
            # a. Call TargetServiceResolver.resolve(strategy_id)
            target_service = TargetServiceResolver.resolve(strategy_id)

            # b. Build the base URL from the resolved service
            base_url = (
                self.ta_bot_url
                if target_service == ServiceType.TA_BOT
                else self.realtime_strategies_url
            )

            # c. Build the payload with parameters, changed_by, reason, validate_only
            params_dict = {}
            if decision.param_change:
                params_dict = {
                    decision.param_change.param: decision.param_change.new_value
                }

            payload = {
                "parameters": params_dict,
                "changed_by": "petrosa-cio",
                "reason": decision.justification
                or "CIO automated parameter adjustment",
                "validate_only": False,
            }

            # d. Await the POST call (unless in DRY_RUN mode)
            url = f"{base_url}/api/v1/strategies/{strategy_id}/config"
            if is_dry_run:
                logger.info(
                    f"[SHADOW MODE] Would have applied parameter change via REST to {url}",
                    extra={
                        "correlation_id": correlation_id,
                        "strategy_id": strategy_id,
                        "payload": payload,
                    },
                )
            else:
                try:
                    response = await self.http_client.post(url, json=payload)

                    # e. If response status >= 400: log FAILED_TO_APPLY
                    if response.status_code >= 400:
                        logger.error(
                            "FAILED_TO_APPLY parameter change for %s. Status: %s, Body: %s",
                            strategy_id,
                            response.status_code,
                            response.text,
                            extra={"correlation_id": correlation_id},
                        )
                    else:
                        # f. If response status 2xx: log SUCCESS, then set param freeze in Redis
                        logger.info(
                            "SUCCESS: Parameter change applied via REST to %s",
                            strategy_id,
                            extra={"correlation_id": correlation_id},
                        )
                        if self.cache:
                            freeze_key = f"cio:freeze:{strategy_id}"
                            await self.cache.set(freeze_key, "LOCKED", ttl=1800)
                            logger.info(
                                "Param freeze set for %s (1800s)",
                                strategy_id,
                                extra={"correlation_id": correlation_id},
                            )
                        else:
                            logger.warning(
                                "FREEZE_SKIPPED: cache unavailable for strategy %s. "
                                "Feedback loop protection is inactive for this change.",
                                strategy_id,
                                extra={"correlation_id": correlation_id},
                            )
                except Exception as e:
                    logger.error(
                        "Error applying parameter change via REST: %s",
                        str(e),
                        extra={"correlation_id": correlation_id},
                    )

        elif action == ActionType.PAUSE_STRATEGY:
            # a. Resolve service using TargetServiceResolver
            target_service = TargetServiceResolver.resolve(strategy_id)

            # b. Build base URL from resolved service
            base_url = (
                self.ta_bot_url
                if target_service == ServiceType.TA_BOT
                else self.realtime_strategies_url
            )

            # c. Payload must be exactly:
            payload = {
                "parameters": {"enabled": False},
                "changed_by": "petrosa-cio",
                "reason": "CIO_PAUSE: " + (decision.justification or "automated pause"),
                "validate_only": False,
            }

            # d. Await the POST call to /api/v1/strategies/{strategy_id}/config
            url = f"{base_url}/api/v1/strategies/{strategy_id}/config"
            if is_dry_run:
                logger.info(
                    f"[SHADOW MODE] Would have paused strategy via REST to {url}",
                    extra={
                        "correlation_id": correlation_id,
                        "strategy_id": strategy_id,
                        "payload": payload,
                    },
                )
            else:
                try:
                    response = await self.http_client.post(url, json=payload)

                    # e. If response status >= 400: log FAILED_TO_APPLY
                    if response.status_code >= 400:
                        logger.error(
                            "FAILED_TO_APPLY strategy pause for %s. Status: %s, Body: %s",
                            strategy_id,
                            response.status_code,
                            response.text,
                            extra={"correlation_id": correlation_id},
                        )
                    else:
                        # f. If response 2xx: log SUCCESS
                        logger.info(
                            "SUCCESS: Strategy %s paused via REST",
                            strategy_id,
                            extra={"correlation_id": correlation_id},
                        )
                except Exception as e:
                    logger.error(
                        "Error applying strategy pause via REST: %s",
                        str(e),
                        extra={"correlation_id": correlation_id},
                    )
        elif action == ActionType.ESCALATE:
            dispatch_tasks_data.append(
                (f"cio.escalation.{strategy_id}", decision.model_dump_json().encode())
            )
        elif action == ActionType.RETRY_SAFE:
            # Proactive retry signal for transient timeouts
            dispatch_tasks_data.append(
                (f"cio.retry.{strategy_id}", decision.model_dump_json().encode())
            )
        elif action == ActionType.FAIL_SAFE:
            # 1. NATS Failure Signal
            dispatch_tasks_data.append(
                (f"cio.failure.{strategy_id}", decision.model_dump_json().encode())
            )
            # 2. Trigger Strategy Pause via REST (Double-lock)
            target_service = TargetServiceResolver.resolve(strategy_id)
            base_url = (
                self.ta_bot_url
                if target_service == ServiceType.TA_BOT
                else self.realtime_strategies_url
            )
            url = f"{base_url}/api/v1/strategies/{strategy_id}/config"
            payload = {
                "parameters": {"enabled": False},
                "changed_by": "petrosa-cio",
                "reason": "CRITICAL_FAIL_SAFE: "
                + (decision.justification or "system failure"),
                "validate_only": False,
            }
            if not is_dry_run:
                try:
                    # We don't await here to not block the NATS publish
                    asyncio.create_task(self.http_client.post(url, json=payload))
                except Exception as e:
                    logger.error(f"Failed to fire fail-safe REST pause: {e}")

        # 3. Handle Dispatch execution (Checking DRY_RUN)
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
            logger.info(
                "T-Junction dispatch successful",
                extra={
                    "correlation_id": correlation_id,
                    "action": action.value,
                    "strategy_id": strategy_id,
                    "targets": [t[0] for t in dispatch_tasks_data],
                },
            )
