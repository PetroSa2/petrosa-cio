import asyncio
import json
import logging
import os
from typing import Any, Protocol

import httpx

from cio.core.alerting.fr66_alerts import (
    CIO_ALERT_ACTIONS,
    build_cio_action_alert,
    cio_action_subject,
    publish_fr66_alert,
)
from cio.core.cache import AsyncRedisCache
from cio.core.decision_store import DecisionRecord, DecisionStore
from cio.core.leverage_arbiter import arbitrate_leverage
from cio.core.service_resolver import ServiceType, TargetServiceResolver
from cio.core.vector import VectorClientProtocol
from cio.models import ActionType, DecisionResult, TriggerContext
from cio.output.translator import TradeEngineTranslator

try:
    from petrosa_otel import inject_trace_context as _inject_trace_context

    _CIO_NATS_INJECT = True
except ImportError:
    _CIO_NATS_INJECT = False
    _inject_trace_context = None

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

# P1.4-AC5 (#134, FR59): in-position governance actions. Each emits on
# `cio.position.<action_value>.<strategy_id>` with the full DecisionResult
# payload — downstream `petrosa-tradeengine` (consumer-side ticket, filed
# as the AC5.c follow-up) translates each subject into a Binance order
# modification / exit / partial-close. Audit copy is the generic
# `cio.decision.audit.<action>` already emitted further down (AC6.a).
_POSITION_ACTIONS = frozenset(
    {
        ActionType.MODIFY_STOPS,
        ActionType.EXIT_NOW,
        ActionType.SCALE_OUT,
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
        decision_store: "DecisionStore | None" = None,
    ):
        self.nats_client = nats_client
        self.vector_client = vector_client
        # Per-action authority (P1.3, #115). When None, the router behaves as
        # if every action were ENABLED — preserving pre-P1.3 behavior and
        # keeping the construction surface backwards-compatible.
        self.authority_store = authority_store
        self.decision_store = decision_store
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

    @staticmethod
    def _resolve_routing_strategy_id(
        context: TriggerContext, fallback_strategy_id: str
    ) -> str:
        """
        petrosa-cio#200: prefer the canonical strategy id carried in the
        original trigger payload's ``metadata.strategy_id`` (set by
        producers such as petrosa-realtime-strategies) over the top-level
        field, which may be a human display name (e.g. "Iceberg Order
        Detector" instead of "iceberg_detector"). Falls back to
        ``fallback_strategy_id`` (context.strategy_id) when no valid
        metadata override is present. Defensive against test doubles where
        ``trigger_payload`` is not a real dict.
        """
        payload = getattr(context, "trigger_payload", None)
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        candidate = metadata.get("strategy_id") if isinstance(metadata, dict) else None
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        return fallback_strategy_id

    def _resolve_base_url_for_dispatch(
        self,
        *,
        context: TriggerContext,
        strategy_id: str,
        correlation_id: str,
        action_name: str,
    ) -> str | None:
        """
        Resolves TargetServiceResolver -> base_url for a REST dispatch
        branch. Returns None (and logs an error) when the strategy cannot
        be routed to either known service — callers MUST skip the REST
        call in that case rather than guessing a target (petrosa-cio#200):
        defaulting an unrecognised strategy to a concrete service can route
        freeze/pause actions at the wrong service.
        """
        routing_id = self._resolve_routing_strategy_id(context, strategy_id)
        target_service = TargetServiceResolver.resolve(routing_id)

        if target_service == ServiceType.UNKNOWN:
            logger.error(
                "UNROUTABLE_STRATEGY: cannot resolve target service for '%s' "
                "(routing id '%s') during %s; skipping REST dispatch to "
                "avoid misrouting to the wrong service.",
                strategy_id,
                routing_id,
                action_name,
                extra={"correlation_id": correlation_id, "strategy_id": strategy_id},
            )
            return None

        return (
            self.ta_bot_url
            if target_service == ServiceType.TA_BOT
            else self.realtime_strategies_url
        )

    async def route(self, context: TriggerContext, decision: DecisionResult) -> None:
        """
        Routes the decision following the T-Junction logic:
        1. Legacy Path -> Translated Signal -> signals.trading (Only for EXECUTE)
        2. Audit Path -> DecisionResult + Context -> Vector DB (Always enabled for all actions)

        petrosa-cio#215: a "Modern Path" branch used to also publish the raw
        DecisionResult on `trade.execute.{id}` for EXECUTE actions. It had zero
        subscribers anywhere in the ecosystem (tradeengine only consumes
        `signals.trading.*`; data-manager only consumes `cio.intent.>` and
        `signals.trading.>`) and was removed. Nothing was lost: the audit path
        below already persists the full `DecisionResult` (including
        `rejection_source`, `thought_trace`, `decided_leverage`) via
        `vector_client.upsert`, which is the same payload the dead branch used
        to publish.
        """
        correlation_id = context.correlation_id
        decision_id = context.decision_id
        # petrosa-cio#211: producers sometimes emit a human display name
        # ("Iceberg Order Detector  625") instead of a canonical snake_case
        # id. NATS subjects cannot contain whitespace — the parser splits on
        # it and rejects the PUB (processPub Parse Error), silently dropping
        # every downstream subject built from this value (cio.retry.*,
        # signals.trading.*, cio.escalation.*, cio.weight.*,
        # cio.throttle.*, cio.veto.*, cio.lifecycle.*, cio.position.*,
        # cio.failure.*). Reuse the same normaliser already applied to REST
        # routing (TargetServiceResolver._normalize, petrosa-cio#200) once
        # here so every subject built below from `strategy_id` is guaranteed
        # whitespace-free.
        strategy_id = (
            TargetServiceResolver._normalize(context.strategy_id)
            if context.strategy_id
            else context.strategy_id
        )
        action = decision.action or ActionType.SKIP
        is_dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

        # P1.5-AC3 (#137) / #174 — resolve the admission-time leverage
        # decision ONCE per routed decision so the SAME `decided_leverage`
        # value reaches the outbound legacy Signal (translator, below), the
        # decision-store audit row, and (via the dashboard endpoint) the
        # operator UI. Previously this was computed only inside the
        # decision_store block — AFTER the translator had already run — so
        # `decided_leverage` never made it onto the dispatched Signal (#174).
        # Defensive isinstance guards keep this safe against
        # `MagicMock(spec=TriggerContext)` test doubles, which auto-vivify
        # `context.recommended_leverage` as a Mock object rather than
        # raising AttributeError (so a plain `getattr(..., None)` would not
        # fall back to None for those doubles).
        _raw_recommended_leverage = getattr(context, "recommended_leverage", None)
        _raw_strategy_envelope = getattr(context, "strategy_leverage_envelope", None)
        leverage_decision = arbitrate_leverage(
            recommended_leverage=(
                _raw_recommended_leverage
                if isinstance(_raw_recommended_leverage, int)
                else None
            ),
            strategy_envelope=(
                _raw_strategy_envelope
                if isinstance(_raw_strategy_envelope, int)
                else None
            ),
        )
        decision.decided_leverage = leverage_decision.decided_leverage

        # 0. Record Metrics
        from cio.core.metrics import DECISION_ACTIONS

        DECISION_ACTIONS.add(
            1, {"action_type": action.value, "strategy_id": strategy_id}
        )

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
                if _CIO_NATS_INJECT and _inject_trace_context is not None:
                    legacy_data = _inject_trace_context(legacy_data)
                dispatch_tasks_data.append(
                    (legacy_subject, json.dumps(legacy_data).encode())
                )

        elif action == ActionType.MODIFY_PARAMS:
            # a. Resolve the base URL via TargetServiceResolver (petrosa-cio#200:
            # returns None and logs when the strategy can't be routed).
            base_url = self._resolve_base_url_for_dispatch(
                context=context,
                strategy_id=strategy_id,
                correlation_id=correlation_id,
                action_name="MODIFY_PARAMS",
            )

            if base_url is not None:
                # b. Build the payload with parameters, changed_by, reason, validate_only
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

                # c. Await the POST call (unless in DRY_RUN mode)
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

                        # petrosa-cio#214 (defect 4): a 2xx status does not
                        # mean success — producers can report a rejected
                        # change in the body. Check both signals before
                        # treating this as SUCCESS.
                        body_failed, body_error = self._response_reports_failure(
                            response
                        )

                        # d. If response status >= 400 OR the body reports
                        # failure: log FAILED_TO_APPLY, do NOT set the
                        # freeze lock — leave CIO free to retry.
                        if response.status_code >= 400 or body_failed:
                            logger.error(
                                "FAILED_TO_APPLY parameter change for %s. Status: %s, "
                                "Body: %s, body_reported_failure=%s, body_error=%s",
                                strategy_id,
                                response.status_code,
                                response.text,
                                body_failed,
                                body_error,
                                extra={
                                    "correlation_id": correlation_id,
                                    "body_reported_failure": body_failed,
                                },
                            )

                            # Handle 429 specifically (AC2, AC4)
                            if response.status_code == 429:
                                await self._apply_rate_limit_freeze(
                                    strategy_id, correlation_id, response
                                )
                        else:
                            # e. If response status 2xx AND the body agrees
                            # (or is silent): log SUCCESS, then set param
                            # freeze in Redis
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
            # a. Resolve the base URL via TargetServiceResolver (petrosa-cio#200:
            # returns None and logs when the strategy can't be routed).
            base_url = self._resolve_base_url_for_dispatch(
                context=context,
                strategy_id=strategy_id,
                correlation_id=correlation_id,
                action_name="PAUSE_STRATEGY",
            )

            if base_url is not None:
                # b. Payload must be exactly:
                payload = {
                    "parameters": {"enabled": False},
                    "changed_by": f"petrosa-cio:{strategy_id}",
                    "reason": "CIO_PAUSE: "
                    + (decision.justification or "automated pause"),
                    "validate_only": False,
                }

                # c. Await the POST call to /api/v1/strategies/{strategy_id}/config
                url = f"{base_url}/api/v1/strategies/{strategy_id}/config"
                # AC4 (cio#169): Skip POST if already frozen — prevents 429 storms when LLM
                # repeatedly decides pause_strategy for the same strategy within the freeze window.
                _pause_freeze_key = f"cio:freeze:{strategy_id}"
                _pause_already_frozen = bool(
                    self.cache and await self.cache.get(_pause_freeze_key)
                )
                if _pause_already_frozen:
                    logger.info(
                        "PAUSE_SKIPPED: strategy %s already frozen — dedup active",
                        strategy_id,
                        extra={"correlation_id": correlation_id},
                    )
                elif is_dry_run:
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

                        # petrosa-cio#214 (defect 4): same body-vs-status gap
                        # as MODIFY_PARAMS — a 2xx status does not mean the
                        # pause was actually accepted.
                        body_failed, body_error = self._response_reports_failure(
                            response
                        )

                        # d. If response status >= 400 OR the body reports
                        # failure: log FAILED_TO_APPLY, do NOT set the
                        # freeze lock — leave CIO free to retry the pause.
                        if response.status_code >= 400 or body_failed:
                            logger.error(
                                "FAILED_TO_APPLY strategy pause for %s. Status: %s, "
                                "Body: %s, body_reported_failure=%s, body_error=%s",
                                strategy_id,
                                response.status_code,
                                response.text,
                                body_failed,
                                body_error,
                                extra={
                                    "correlation_id": correlation_id,
                                    "body_reported_failure": body_failed,
                                },
                            )

                            # Handle 429 specifically (AC2, AC4)
                            if response.status_code == 429:
                                await self._apply_rate_limit_freeze(
                                    strategy_id, correlation_id, response
                                )
                        else:
                            # e. If response 2xx AND the body agrees (or is
                            # silent): log SUCCESS, then set freeze in
                            # Redis (AC3)
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
                        # #209 (AC4): several httpx exceptions (and some
                        # connection-reset errors) stringify to "" — logging
                        # bare str(e) then produces the misleading empty-tail
                        # "Error applying strategy pause via REST: " line
                        # that gives on-call zero signal. Same pattern as
                        # context_builder.py's _fetch_strategy_stats /
                        # _fetch_regime exc_type+detail logging (#197).
                        exc_type = type(e).__name__
                        detail = str(e) or "<empty>"
                        logger.error(
                            "Error applying strategy pause via REST: "
                            "exc_type=%s detail=%s",
                            exc_type,
                            detail,
                            extra={
                                "correlation_id": correlation_id,
                                "exc_type": exc_type,
                            },
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
        elif action in _POSITION_ACTIONS:
            # P1.4-AC5 (#134, FR59): in-position governance actions emit on
            # `cio.position.<kind>.<sid>`. tradeengine subscribers translate
            # each subject into a Binance order modification / market exit /
            # partial-close. The producer side is owned here; the consumer
            # handlers in tradeengine are the AC5.c follow-up child ticket.
            dispatch_tasks_data.append(
                (
                    f"cio.position.{action.value}.{strategy_id}",
                    decision.model_dump_json().encode(),
                )
            )
        elif action == ActionType.FAIL_SAFE:
            # 1. NATS Failure Signal
            dispatch_tasks_data.append(
                (f"cio.failure.{strategy_id}", decision.model_dump_json().encode())
            )
            # 2. Trigger Strategy Pause via REST (Double-lock). petrosa-cio#200:
            # skip the REST call (rather than guessing a target) when the
            # strategy can't be routed — the NATS failure signal above still
            # fires either way.
            base_url = self._resolve_base_url_for_dispatch(
                context=context,
                strategy_id=strategy_id,
                correlation_id=correlation_id,
                action_name="FAIL_SAFE",
            )
            if base_url is not None:
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

        # Record in the dashboard decision store (#654).
        if self.decision_store is not None:
            _confidence_map = {"HIGH": 0.9, "MEDIUM": 0.6, "LOW": 0.3}
            # FR53 / P3.4 (#130): surface structured rejection_source +
            # strategy_revision_id so the operator dashboard can show "why" +
            # which revision the intent claimed vs the one characterizations
            # are bound to.
            rejection_source_value = (
                decision.rejection_source.value
                if decision.rejection_source is not None
                else None
            )
            # P1.4-AC4 (#132): carry the PreDecisionContext snapshot into the
            # ring buffer so /api/dashboard/decisions/recent can return it
            # verbatim. Pre-EPIC-#122 historical records have no bundle —
            # the dashboard renders them with null context.
            # P1.5-AC3 (#137) / #174 — `leverage_decision` was already
            # resolved once at the top of `route()` (single source of truth
            # shared with the translator dispatch below); reuse it here
            # rather than recomputing.
            logger.info(
                "leverage_arbiter decision_id=%s branch=%s decided=%s bound=%s",
                decision_id,
                leverage_decision.branch,
                leverage_decision.decided_leverage,
                leverage_decision.per_strategy_bound,
                extra={"correlation_id": correlation_id},
            )

            self.decision_store.record(
                DecisionRecord(
                    strategy_id=strategy_id,
                    action=action.value,
                    reasoning_trace=(
                        decision.thought_trace or decision.justification or ""
                    ),
                    confidence=_confidence_map.get(
                        getattr(decision.regime_confidence, "value", "").upper(), 0.5
                    ),
                    decision_id=decision_id,
                    rejection_source=rejection_source_value,
                    strategy_revision_id=getattr(context, "strategy_revision_id", None),
                    pre_decision_context=getattr(context, "pre_decision_context", None),
                    decided_leverage=leverage_decision.decided_leverage,
                )
            )

        # P8-AC2b (#139): emit `alerts.cio.<action>.<strategy_id>` for every
        # governance ActionType (VETO / DEMOTE / RETIRE / EXIT_NOW). Best-effort
        # — fires AFTER the decision_store record so the alert is the last side
        # effect of dispatch; an unhealthy NATS does not block the decision
        # path. Lower-cased action_value matches the subject family in
        # `cio/core/alerting/fr66_alerts.py::CIO_ALERT_ACTIONS`.
        action_value = action.value.lower()
        if action_value in CIO_ALERT_ACTIONS and not is_dry_run:
            alert_payload = build_cio_action_alert(
                action=action_value,
                strategy_id=strategy_id,
                decision_id=decision_id,
                justification=(decision.thought_trace or decision.justification or ""),
            )
            await publish_fr66_alert(
                self.nats_client,
                subject=cio_action_subject(action_value, strategy_id),
                payload=alert_payload,
            )

        # petrosa-cio#215: `cio.context.gap.<surface>` used to be published here
        # per P1.4-AC2.b (#132) on the (incorrect) claim that "data-manager's
        # FR12 audit-trail consumer subscribes to `cio.context.gap.>` and
        # persists each event keyed by decision_id". That consumer was never
        # built (data-manager's subscriber inventory has no `cio.context.gap.*`
        # entry) and CIO's own test admitted the scope gap explicitly. The
        # publish was removed rather than wired to a real consumer because the
        # same gap data (`pre_decision_context.gaps`) is already captured
        # without NATS via `decision_store.record(...)` a few lines below,
        # which feeds `/api/dashboard/decisions/recent` — so removing this
        # dead publish loses no data. If a real FR12 NATS-driven audit-trail
        # consumer is wanted in data-manager, file it as a follow-up feature
        # ticket rather than resurrecting an unconsumed publish.

    @staticmethod
    def _response_reports_failure(response: httpx.Response) -> tuple[bool, Any]:
        """Body-vs-status failure detection (petrosa-cio#214, defect 4).

        Producers (ta_bot/api/config_routes.py, realtime-strategies
        strategies/api/config_routes.py) return HTTP 200 with a
        ``{"success": false, "error": {...}}`` body on validation failure —
        the REST framework never surfaces this as a 4xx. Relying on
        ``status_code`` alone made a REJECTED parameter change look
        identical to an ACCEPTED one: CIO logged SUCCESS, set a 30-minute
        ``cio:freeze:`` lock, and stopped retrying, while the strategy kept
        running its old parameters — silent divergence between CIO's model
        of the world and reality.

        Returns ``(reports_failure, error_detail)``. A non-JSON body, a
        JSON body that isn't a dict, or a dict without a ``"success"`` key
        are all treated as "no body opinion" (``reports_failure=False``) —
        this check only ever makes failure detection LOUDER than the
        pre-existing status-code check, never more silent. Callers should
        OR this with the status-code check, not replace it.
        """
        try:
            body = response.json()
        except Exception:
            return False, None
        if not isinstance(body, dict) or "success" not in body:
            return False, None
        if body.get("success"):
            return False, None
        return True, body.get("error")

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
