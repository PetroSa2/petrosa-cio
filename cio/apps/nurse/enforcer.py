import asyncio
import logging
import time

from cio.core.alerting.manager import AlertManager
from cio.core.orchestrator import Orchestrator
from cio.models import (
    CRITICAL_FAILURE_RESULT,
    TIMEOUT_RETRY_RESULT,
    DecisionResult,
    TriggerContext,
)

# Optional OpenTelemetry imports
try:
    from opentelemetry.trace import StatusCode
    from opentelemetry.trace.status import Status
    from petrosa_otel import get_tracer

    tracer = get_tracer("petrosa-cio")
except ImportError:
    tracer = None
    StatusCode = None
    Status = None

logger = logging.getLogger(__name__)


class NurseEnforcer:
    """
    Enforces strict audit rules on trading intents.
    Wrapped in a 200ms timeout guard to ensure low-latency fail-safe.
    """

    def __init__(self, orchestrator: Orchestrator):
        self.orchestrator = orchestrator

    async def audit(self, context: TriggerContext) -> DecisionResult:
        """
        Executes the reasoning loop via Orchestrator with a 200ms timeout guard.
        If the timeout is hit, returns RETRY_SAFE to the fleet.
        If a critical exception occurs, returns FAIL_SAFE.
        """
        start_time = time.perf_counter()
        correlation_id = context.correlation_id

        # AC 4: Latency Instrumentation via petrosa-otel span
        if tracer:
            span = tracer.start_span("cio.nurse.audit")
            span.set_attribute("correlation_id", correlation_id)
            span.set_attribute("symbol", context.trigger_payload.get("symbol", "N/A"))
        else:
            span = None

        try:
            # AC 1: Wrap in a 200ms timeout guard
            # AC 2: Return standardized RETRY_SAFE on timeout
            decision = await asyncio.wait_for(
                self.orchestrator.run(context), timeout=0.2
            )

            # Record final latency
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            logger.info(
                "Audit successful",
                extra={
                    "correlation_id": correlation_id,
                    "latency_ms": latency_ms,
                    "action": str(decision.action),
                },
            )

            if span:
                span.set_attribute("latency_ms", latency_ms)
                span.set_attribute("action", str(decision.action))
                span.end()

            return decision

        except asyncio.TimeoutError:  # noqa: UP041
            # AC 2: Proactively returns RETRY_SAFE
            latency_ms = int((time.perf_counter() - start_time) * 1000)

            # AC 3: Dispatch RED alert upon timeout (backgrounded to keep enforcer responsive)
            asyncio.create_task(
                AlertManager.dispatch_critical_alert(
                    f"Audit Timeout: Exceeded 200ms ({latency_ms}ms)",
                    context={
                        "correlation_id": correlation_id,
                        "latency_ms": latency_ms,
                        "strategy_id": context.strategy_id,
                    },
                )
            )

            if span and Status and StatusCode:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("latency_ms", latency_ms)
                span.set_attribute("timeout", True)
                span.end()

            return TIMEOUT_RETRY_RESULT

        except Exception as e:
            # AC 2: Standardized FAIL_SAFE for other critical failures
            logger.exception(
                f"CRITICAL_ENFORCER_FAILURE: {str(e)}",
                extra={"correlation_id": correlation_id},
            )

            if span and Status and StatusCode:
                span.set_status(Status(StatusCode.ERROR))
                span.record_exception(e)
                span.end()

            return CRITICAL_FAILURE_RESULT
