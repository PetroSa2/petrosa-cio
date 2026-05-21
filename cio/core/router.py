import asyncio
import json
import logging
import os
from typing import Any, Protocol

import httpx

from cio.core.cache import AsyncRedisCache
from cio.core.service_resolver import ServiceType, TargetServiceResolver
from cio.core.vector import VectorClientProtocol
from cio.models import ActionType, DecisionResult, TriggerContext
from cio.output.translator import TradeEngineTranslator

logger = logging.getLogger(__name__)


# Lifecycle ActionType values (per #114 P1.2). Kept as a module-level set so the
# router's elif chain can use a single membership check instead of six branches.
_LIFECYCLE_ACTIONS = frozenset(
    {
        ActionType.ADMIT,
        ActionType.ADMIT_SMALL,
        ActionType.REJECT,
        ActionType.PROMOTE,
        ActionType.DEMOTE,
        ActionType.RETIRE,
    }
)


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
        cache: AsyncRedisCache | None = None,
        authority_store: Any = None,
    ):
        self.nats_client = nats_client
        self.vector_client = vector_client
        # Per-action authority (P1.3, #115). When None, the router behaves as
        # if every action were ENABLED — preserving pre-P1.3 behavior and
        # keeping the construction surface backwards-compatible.
        self.authority_store = authority_store
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
        decision_id = context.decision_id
        strategy_id = context.strategy_id
        action = decision.action or ActionType.SKIP
        is_dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

        # 0. Record Metrics
        from cio.core.metrics import DECISION_ACTIONS

        DECISION_ACTIONS.labels(action_type=action.value, strategy_id=strategy_id).inc()

        # Set OTel decision context attributes on the current span
        try:
            from opentelemetry import trace as _trace
            from petrosa_otel import set_decision_context

            _span = _trace.get_current_span()
            set_decision_context(
                _span,
                decision_id=decision_id,
                strategy_id=strategy_id,
                action=action.value,
                symbol=context.trigger_payload.get("symbol", ""),
                correlation_id=correlation_id,
            )
        except ImportError:
            pass
        except Exception as _otel_exc:
            logger.debug("set_decision_context failed: %s", _otel_exc)

        # 0b. Apply per-action authority (P1.3, #115). When configured, may:
        #     * divert to the pending-approval queue (returns early, no dispatch)
        #     * substitute the action with a next-best safe fallback
        original_action = action
        authority_pending = None
        authority_was_disabled = False
        if self.authority_store is not None:
            from cio.core.authority import apply_authority

            authority_decision = apply_authority(
                self.authority_store,
                action=action,
                strategy_id=strategy_id,
                decision_id=decision_id,
                correlation_id=correlation_id,
                context_payload=context.trigger_payload,
                decision_payload=decision.model_dump(),
            )
            if authority_decision.pending is not None:
                # Divert: record the diversion in the audit trail and stop.
                authority_pending = authority_decision.pending
                await self.vector_client.upsert(
                    strategy_id=strategy_id,
                    payload={
                        "event_type": "decision_pending_approval",
                        "action": original_action.value,
                        "correlation_id": correlation_id,
                        "decision_id": decision_id,
                        "queue_id": authority_pending.queue_id,
                        "summary": decision.justification,
                        "thought_trace": decision.thought_trace,
                        "decision_data": decision.model_dump(),
                    },
                )
                logger.info(
                    "Decision diverted to operator-approval queue",
                    extra={
                        "correlation_id": correlation_id,
                        "action": original_action.value,
                        "strategy_id": strategy_id,
                        "queue_id": authority_pending.queue_id,
                    },
                )
                return
            if authority_decision.was_disabled:
                action = authority_decision.action
                authority_was_disabled = True
                logger.info(
                    "Action substituted by authority fallback",
                    extra={
                        "correlation_id": correlation_id,
                        "original_action": original_action.value,
                        "fallback_action": action.value,
                        "strategy_id": strategy_id,
                    },
                )

        # 1. Prepare Audit Path (Memory storage - Always executed)
        audit_payload: dict[str, Any] = {
            "event_type": "decision",
            "action": action.value,
            "correlation_id": correlation_id,
            "decision_id": decision_id,
            "summary": decision.justification,
            "thought_trace": decision.thought_trace,
            "decision_data": decision.model_dump(),
        }
        if authority_was_disabled:
            audit_payload["authority_fallback_from"] = original_action.value
        audit_task = self.vector_client.upsert(
            strategy_id=strategy_id,
            payload=audit_payload,
        )

        # 2. Prepare NATS Dispatch Path (T-Junction)
        dispatch_tasks_data: list[tuple[str, bytes]] = []

        if action == ActionType.EXECUTE:
            # LEGACY BRANCH: Translate to Signal model and send to legacy topic.
            # Contract: petrosa-tradeengine subscribes to signals.trading.> (wildcard after
            # base). Bare "signals.trading" is NOT matched by that subscription in NATS.
            # Align with TA bot / docs: f"{base_topic}.{strategy_id}".
            base_signals = (
                os.getenv("NATS_TOPIC_SIGNALS") or "signals.trading"
            ).rstrip(".*>")
            legacy_subject = f"{base_signals}.{strategy_id}"
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
                "changed_by": f"petrosa-cio:{strategy_id}",
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

                        # Handle 429 specifically (AC2, AC4)
                        if response.status_code == 429:
                            await self._apply_rate_limit_freeze(
                                strategy_id, correlation_id, response
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
                "changed_by": f"petrosa-cio:{strategy_id}",
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

                        # Handle 429 specifically (AC2, AC4)
                        if response.status_code == 429:
                            await self._apply_rate_limit_freeze(
                                strategy_id, correlation_id, response
                            )
                    else:
                        # f. If response 2xx: log SUCCESS, then set freeze in Redis (AC3)
                        logger.info(
                            "SUCCESS: Strategy %s paused via REST",
                            strategy_id,
                            extra={"correlation_id": correlation_id},
                        )
                        if self.cache:
                            freeze_key = f"cio:freeze:{strategy_id}"
                            await self.cache.set(freeze_key, "LOCKED", ttl=1800)
                            logger.info(
                                "Pause freeze set for %s (1800s)",
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
        elif action == ActionType.DOWN_WEIGHT:
            # Governance: reduce strategy's per-decision allocation. Subscribers
            # (lifecycle authority, dashboard) consume cio.weight.{strategy_id}
            # to adjust per-decision sizing without halting signal flow.
            dispatch_tasks_data.append(
                (f"cio.weight.{strategy_id}", decision.model_dump_json().encode())
            )
        elif action == ActionType.THROTTLE:
            # Governance: rate-limit a strategy's signals over a window.
            dispatch_tasks_data.append(
                (f"cio.throttle.{strategy_id}", decision.model_dump_json().encode())
            )
        elif action == ActionType.VETO:
            # Governance: reject this specific intent without changing the strategy's
            # standing weight. Distinct from SKIP in that downstream subscribers are
            # notified (audit/dashboard) instead of silently dropping the intent.
            dispatch_tasks_data.append(
                (f"cio.veto.{strategy_id}", decision.model_dump_json().encode())
            )
        elif action in _LIFECYCLE_ACTIONS:
            # Lifecycle (per #114 P1.2): every transition emitted by the strategy
            # lifecycle state machine publishes on `cio.lifecycle.<kind>.<sid>`.
            # Subscribers (data-manager audit-trail, dashboard, lifecycle reader)
            # observe the standing-state changes without touching the per-intent
            # signal path.
            dispatch_tasks_data.append(
                (
                    f"cio.lifecycle.{action.value}.{strategy_id}",
                    decision.model_dump_json().encode(),
                )
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
                "changed_by": f"petrosa-cio:{strategy_id}",
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

        # 2b. Audit copy on cio.decision.audit.<action> — feeds the CIO
        # health evaluator (P7.1, #610) and is the Phase-2 substrate for
        # decision/outcome correlation. Published for *every* action so
        # the evaluator can compute reasoning-context presence and
        # FAIL_SAFE/SKIP dominance over a sliding window.
        audit_copy_payload = {
            "decision_id": decision_id,
            "correlation_id": correlation_id,
            "strategy_id": strategy_id,
            "action": action.value,
            "thought_trace": decision.thought_trace,
            "justification": decision.justification,
        }
        if authority_was_disabled:
            audit_copy_payload["authority_fallback_from"] = original_action.value
        dispatch_tasks_data.append(
            (
                f"cio.decision.audit.{action.value}",
                json.dumps(audit_copy_payload).encode(),
            )
        )

        # 3. Handle Dispatch execution (Checking DRY_RUN)
        nats_publish_tasks = []

        for subject, msg_bytes in dispatch_tasks_data:
            if is_dry_run:
                logger.info(
                    f"[SHADOW MODE] Would have published to {subject}",
                    extra={
                        "correlation_id": correlation_id,
                        "action": action.value,
                        "strategy_id": strategy_id,
                        "payload_preview": msg_bytes.decode()[:300],
                    },
                )
            else:
                nats_publish_tasks.append(self.nats_client.publish(subject, msg_bytes))

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

    async def _apply_rate_limit_freeze(
        self, strategy_id: str, correlation_id: str, response: httpx.Response
    ) -> None:
        """Helper to parse 429 retry-after and set Redis freeze with clamping."""
        if not self.cache:
            return

        retry_after = 3600
        try:
            body = response.json()
            raw_val = body.get("retry_after", 3600)
            # Coerce and clamp (AC2, PR Review)
            retry_after = int(float(raw_val))
            retry_after = max(1, min(retry_after, 86400))  # 1s to 24h
        except Exception:
            pass

        freeze_key = f"cio:freeze:{strategy_id}"
        await self.cache.set(freeze_key, "LOCKED", ttl=retry_after)
        logger.info(
            "Rate limit freeze set for %s (%ss) due to 429",
            strategy_id,
            retry_after,
            extra={"correlation_id": correlation_id},
        )
