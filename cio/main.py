import asyncio
import logging
import os
import signal
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI
from nats.aio.client import Client as NATS

from cio.apps.authority_api import router as authority_router
from cio.apps.dashboard_api import router as dashboard_router
from cio.apps.lifecycle_api import router as lifecycle_router
from cio.apps.nurse.enforcer import NurseEnforcer
from cio.apps.state_api import router as state_router
from cio.clients.factory import ClientFactory
from cio.core.alerting.drawdown_breach_emitter import DrawdownBreachEmitter
from cio.core.alerting.envelope_drift_emitter import EnvelopeDriftEmitter
from cio.core.alerts_consumer import AlertsConsumer
from cio.core.arbiter import SignalArbiter
from cio.core.authority import AuthorityStore
from cio.core.auto_resume import (
    AutoResumeLoop,
    LLMHealthTracker,
    LLMPauseRegistry,
    auto_resume_enabled,
    instrument_llm_health,
)
from cio.core.cache import AsyncRedisCache
from cio.core.context_builder import ContextBuilder
from cio.core.decision_store import DecisionStore
from cio.core.evaluator_subscriber import EvaluatorSubscriber
from cio.core.execution_events_consumer import ExecutionEventsConsumer
from cio.core.health_evaluator import CIOHealthEvaluator
from cio.core.heartbeat import HeartbeatPublisher, HeartbeatResponder
from cio.core.lifecycle import StrategyLifecycleStore
from cio.core.listener import NATSListener
from cio.core.orchestrator import Orchestrator
from cio.core.position_review_loop import (
    DEFAULT_REEVAL_INTERVAL_SECONDS,
    PositionReviewLoop,
)
from cio.core.router import OutputRouter
from cio.core.service_resolver import ServiceType

# Optional OpenTelemetry imports
try:
    from petrosa_otel import attach_logging_handler, setup_telemetry
except ImportError:
    setup_telemetry = None
    attach_logging_handler = None


# Configure Logging
class CorrelationIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "correlation_id"):
            record.correlation_id = "SYSTEM"
        return True


# Ensure root logger and all sub-loggers get the filter and correct format
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - [%(correlation_id)s] - %(message)s"
)
handler.setFormatter(formatter)
handler.addFilter(CorrelationIdFilter())

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
# Clear existing handlers to avoid duplicates
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)
root_logger.addHandler(handler)

# #192: litellm's internal `LiteLLM` logger propagates to the root logger at
# INFO (e.g. "LiteLLM completion() model=..." and "LiteLLM:INFO: utils.py:...")
# with no parseable level token, drowning real signal in Grafana/Loki as
# "unknown"-severity noise. Applied at import time (before server start) so
# it is in effect for every litellm.acompletion/aembedding call regardless of
# call order. WARNING/ERROR-level LLM failure signal (cio#187/#189) is
# untouched — only INFO/DEBUG chatter from this specific logger is raised.
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

logger = logging.getLogger("cio-strategist")

# #209 (AC3): backoff wait (seconds) between NATS reconnect attempts. Kept as
# a named constant (rather than nats.py's library default of 2) so the
# disconnected_cb log line and the connect() call can never drift apart.
NATS_RECONNECT_TIME_WAIT_SECONDS = int(
    os.getenv("NATS_RECONNECT_TIME_WAIT_SECONDS", "2")
)

# Initialize FastAPI for health checks
app = FastAPI(title="Petrosa CIO Health")

# Strategy lifecycle store (P1.2, #114) is shared between the HTTP surface and
# the in-process arbitration loop. The store is in-memory today; persistence
# is pluggable behind the StrategyLifecycleStore API.
app.state.lifecycle_store = StrategyLifecycleStore()
app.include_router(lifecycle_router)

# Per-action authority + pending-approval queue (P1.3, #115). The OutputRouter
# consults `app.state.authority_store` at dispatch time; the HTTP surface
# (operator-only) mutates it via the authority_router endpoints.
app.state.authority_store = AuthorityStore()
app.include_router(authority_router)

# Evaluator-driven pause gate (P2.6, #597). The subscriber lives at
# `app.state.evaluator_subscriber` so the /state HTTP routes can read it.
# It's set in `main()` after the NATS client connects — until then the
# attribute is missing and the /state routes report 503.
app.include_router(state_router)

# Dashboard API (#654, P5.1a follow-up). decision_store wired here so it is
# available immediately; OutputRouter also receives the reference in main().
app.state.decision_store = DecisionStore()
app.include_router(dashboard_router)


@app.get("/health/liveness")
async def liveness():
    return {"status": "ok"}


@app.get("/health/readiness")
async def readiness():
    # Basic check for NATS connection
    if hasattr(app.state, "nats_client") and app.state.nats_client.is_connected:
        return {"status": "ok"}
    return {"status": "degraded", "nats": "disconnected"}


def _enforce_prompt_context_contract() -> None:
    """Runtime gate (P1.4-AC3 / FR55-FR58).

    Loads the active reasoning prompt template and validates it against
    :data:`cio.prompts.context_contract.REQUIRED_CONTEXT_SURFACES`. A
    failure raises and prevents the service from coming up, mirroring the
    CI gate in ``tests/unit/test_prompt_contract.py``.
    """
    import os as _os

    import yaml as _yaml

    from cio.prompts.context_contract import validate_prompt

    yaml_path = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)),
        "prompts",
        "action_classifier_v1.yaml",
    )
    with open(yaml_path) as fh:
        data = _yaml.safe_load(fh) or {}
    active_prompt_template = data.get("system_prompt") or ""
    validate_prompt(active_prompt_template)
    logger.info("Prompt-context contract validated for action_classifier_v1.yaml")


async def main():
    # 0. Enforce prompt-context contract before any service wiring (P1.4-AC3).
    _enforce_prompt_context_contract()

    # 1. Setup OpenTelemetry
    if (
        os.getenv("ENABLE_OTEL", "true").lower() in ("true", "1", "yes")
        and setup_telemetry
        and os.getenv("OTEL_NO_AUTO_INIT", "").lower() not in ("1", "true", "yes", "on")
    ):
        try:
            logger.info("Initializing OpenTelemetry for CIO")
            setup_telemetry(
                service_name=os.getenv("OTEL_SERVICE_NAME", "petrosa-cio"),
                service_type="async",
                enable_http=True,
            )
        except Exception as e:
            logger.warning(f"Failed to initialize OpenTelemetry: {e}")

    # 3. Attach OTel logging handler LAST (after logging is configured)
    if (
        os.getenv("ENABLE_OTEL", "true").lower() in ("true", "1", "yes")
        and attach_logging_handler
        and os.getenv("OTEL_NO_AUTO_INIT", "").lower() not in ("1", "true", "yes", "on")
    ):
        try:
            # #192: emit structured JSON stdout logs so Grafana/Loki derive a
            # real severity token instead of falling back to "unknown" for
            # lines the text formatter can't cleanly classify.
            success = attach_logging_handler(use_json_format=True)
            if success:
                logger.info(
                    "✅ OpenTelemetry logging handler attached - logs will be exported to Grafana"
                )
        except Exception as e:
            logger.error(f"Failed to attach OTel logging handler: {e}")

    # 1. Load Configuration
    nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
    # #236: computed up-front (rather than in step 4 below, where it was
    # previously computed inline right before `listener.start()`) so the
    # NATS reconnect-and-resubscribe supervisor added below can replay the
    # exact same subject after a permanent-closure reconnect.
    intents_subject = os.getenv("NATS_TOPIC_INTENTS", "cio.intent.trading")
    if not intents_subject.endswith(">"):
        _base_subject = intents_subject.rstrip(".*")
        subscribe_subject = f"{_base_subject}.>"
    else:
        subscribe_subject = intents_subject
    # #236: graceful-shutdown event, created early so the NATS reconnect
    # supervisor (defined below, before any subscriber exists) can check it
    # without a forward reference.
    stop_event = asyncio.Event()
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    data_manager_url = os.getenv("DATA_MANAGER_URL", "http://petrosa-data-manager:80")
    tradeengine_url = os.getenv(
        "TRADEENGINE_URL", "http://petrosa-tradeengine-service:80"
    )
    ta_bot_url = os.getenv("TA_BOT_URL", "http://petrosa-ta-bot-service:80")
    realtime_strategies_url = os.getenv(
        "REALTIME_STRATEGIES_URL", "http://petrosa-realtime-strategies:80"
    )

    # 2. Initialize Components
    #
    # #209 (AC3): nats.py already retries transport drops internally, but
    # with no callbacks wired the retry/backoff cycle was entirely
    # invisible — a raw `ConnectionResetError: [Errno 104]` from inside
    # the client's read loop only ever reached the logs as an unstructured
    # traceback (see incident 2026-09-18 02:15:56 UTC), with no
    # correlation_id and no indication reconnection was in progress or
    # succeeded. error_cb/disconnected_cb/reconnected_cb/closed_cb route
    # every transport event through the same structured logger as the
    # rest of the service. max_reconnect_attempts=-1 (infinite) plus the
    # explicit reconnect_time_wait backoff means a long-lived transport
    # blip never permanently closes the connection out from under a
    # process that's otherwise healthy.
    async def _nats_error_cb(e: Exception) -> None:
        exc_type = type(e).__name__
        detail = str(e) or "<empty>"
        logger.warning(
            "NATS_TRANSPORT_ERROR: exc_type=%s detail=%s",
            exc_type,
            detail,
            extra={
                "correlation_id": "SYSTEM",
                "exc_type": exc_type,
                "audit_exempt": True,
                "transient": True,
            },
        )

    async def _nats_disconnected_cb() -> None:
        logger.warning(
            "NATS_DISCONNECTED: connection to %s lost, reconnect loop active "
            "(reconnect_time_wait=%ss, max_reconnect_attempts=infinite)",
            nats_url,
            NATS_RECONNECT_TIME_WAIT_SECONDS,
            extra={"correlation_id": "SYSTEM"},
        )

    async def _nats_reconnected_cb() -> None:
        logger.info(
            "NATS_RECONNECTED: connection to %s restored",
            nats_url,
            extra={"correlation_id": "SYSTEM"},
        )

    # #236: prior to this fix, closed_cb only logged — nats.py's own
    # max_reconnect_attempts=-1 budget does NOT cover every path to
    # CLOSED (e.g. Client._process_err() force-closes on a server-sent
    # protocol -ERR regardless of the reconnect budget, see
    # nats/aio/client.py::_process_err -> _close(Client.CLOSED)). Once
    # CLOSED, the client never reconnects itself — the only recovery path
    # was the pod's liveness/readiness probe eventually failing and
    # kubelet restarting the whole process (the 2026-09-23 02:36-02:39
    # incident this ticket reports). `_resubscribe_callbacks` is
    # populated below as each NATS-driven subscriber starts, and
    # `_nats_reconnect_and_resubscribe` replays it after a successful
    # in-process reconnect so the service self-heals without a restart.
    _resubscribe_callbacks: list[tuple[str, Any]] = []
    _nats_reconnect_state: dict[str, Any] = {"task": None}

    async def _nats_reconnect_and_resubscribe() -> None:
        if stop_event.is_set():
            return
        backoff = 1.0
        max_backoff = 30.0
        while not stop_event.is_set():
            try:
                await nc.connect(
                    nats_url,
                    error_cb=_nats_error_cb,
                    disconnected_cb=_nats_disconnected_cb,
                    reconnected_cb=_nats_reconnected_cb,
                    closed_cb=_nats_closed_cb,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=NATS_RECONNECT_TIME_WAIT_SECONDS,
                )
                logger.info(
                    "NATS_RECONNECT_SUCCESS: reconnected to %s after permanent "
                    "closure — replaying %d subscription(s)",
                    nats_url,
                    len(_resubscribe_callbacks),
                    extra={"correlation_id": "SYSTEM"},
                )
                for name, start_cb in _resubscribe_callbacks:
                    try:
                        await start_cb()
                    except Exception as sub_err:
                        logger.error(
                            "NATS_RESUBSCRIBE_FAILED: component=%s exc_type=%s "
                            "detail=%s",
                            name,
                            type(sub_err).__name__,
                            str(sub_err) or "<empty>",
                            extra={"correlation_id": "SYSTEM"},
                        )
                return
            except Exception as e:
                logger.error(
                    "NATS_RECONNECT_ATTEMPT_FAILED: exc_type=%s detail=%s "
                    "next_attempt_in=%.1fs",
                    type(e).__name__,
                    str(e) or "<empty>",
                    backoff,
                    extra={"correlation_id": "SYSTEM"},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _nats_closed_cb() -> None:
        logger.error(
            "NATS_CLOSED: connection to %s permanently closed",
            nats_url,
            extra={"correlation_id": "SYSTEM"},
        )
        if stop_event.is_set():
            return
        existing_task = _nats_reconnect_state.get("task")
        if existing_task is not None and not existing_task.done():
            return  # a reconnect attempt is already in flight
        # Decoupled via create_task (not awaited inline): closed_cb runs
        # inside nats.py's own _close(), which still touches internal
        # client state (_client_id etc.) after do_cbs returns — awaiting a
        # blocking reconnect here would race that cleanup and could
        # clobber a just-restored connection.
        _nats_reconnect_state["task"] = asyncio.create_task(
            _nats_reconnect_and_resubscribe()
        )

    nc = NATS()
    try:
        await nc.connect(
            nats_url,
            error_cb=_nats_error_cb,
            disconnected_cb=_nats_disconnected_cb,
            reconnected_cb=_nats_reconnected_cb,
            closed_cb=_nats_closed_cb,
            max_reconnect_attempts=-1,
            reconnect_time_wait=NATS_RECONNECT_TIME_WAIT_SECONDS,
        )
        logger.info(f"Connected to NATS at {nats_url}")
        app.state.nats_client = nc
    except Exception as e:
        logger.error(f"Failed to connect to NATS: {e}")
        sys.exit(1)

    import redis.asyncio as redis_asyncio

    redis_client = redis_asyncio.from_url(redis_url)
    cache = AsyncRedisCache(redis_client)
    logger.info(f"Connected to Redis at {redis_url}")

    # Factory creates LiteLLMClient or MockLLMClient based on LLM_PROVIDER env
    llm_client = ClientFactory.create()
    llm_health_tracker = LLMHealthTracker()
    instrument_llm_health(llm_client, llm_health_tracker)
    pause_registry = LLMPauseRegistry(cache)

    # Epic 7: Vector Client for COLD Path
    vector_provider = os.getenv("VECTOR_PROVIDER", "mock").lower()
    if vector_provider == "qdrant":
        from cio.core.vector import QdrantVectorClient

        vector_client = QdrantVectorClient()
        logger.info("Initializing QdrantVectorClient for COLD path.")
    else:
        from cio.core.vector import MockVectorClient

        vector_client = MockVectorClient()
        logger.info("Initializing MockVectorClient for development/testing.")

    builder = ContextBuilder(
        data_manager_url=data_manager_url,
        tradeengine_url=tradeengine_url,
        vector_client=vector_client,
    )

    orchestrator = Orchestrator(llm_client=llm_client, cache=cache)
    enforcer = NurseEnforcer(orchestrator=orchestrator)
    router = OutputRouter(
        nats_client=nc,
        vector_client=vector_client,
        ta_bot_url=ta_bot_url,
        realtime_strategies_url=realtime_strategies_url,
        cache=cache,
        decision_store=app.state.decision_store,
        pause_registry=pause_registry,
    )
    # P2.6 (#597): evaluator-verdict subscriber + arbiter pause gate.
    # Started before arbiter construction so the arbiter wires the
    # subscriber reference, not None. Subscription itself begins below
    # via `await evaluator_subscriber.start()` once the NATS connection
    # is live.
    evaluator_subscriber = EvaluatorSubscriber(nats_client=nc)
    app.state.evaluator_subscriber = evaluator_subscriber

    arbiter = None
    if os.getenv("SIGNAL_ARBITRATION_ENABLED", "true").lower() in ("true", "1", "yes"):
        arbiter = SignalArbiter(
            cache=cache,
            evaluator_subscriber=evaluator_subscriber,
            # #175 (FR60/P1.4-AC7): wires this arbiter as the runner for
            # PositionReviewLoop below — it needs the same context
            # assembly / enforcement / routing pipeline the listener uses
            # for a live trade intent.
            context_builder=builder,
            enforcer=enforcer,
            router=router,
        )
        logger.info("Signal arbitration enabled (with P2.6 pause gate).")
    else:
        logger.info("Signal arbitration disabled (SIGNAL_ARBITRATION_ENABLED=false).")

    listener = NATSListener(
        nats_client=nc,
        enforcer=enforcer,
        context_builder=builder,
        router=router,
        arbiter=arbiter,
    )

    # #175 (FR60/P1.4-AC7): in-position re-evaluation loop. Only runs when
    # arbitration is enabled since `arbiter.run_scheduled_review` is its
    # runner — SIGNAL_ARBITRATION_ENABLED=false is the same "off switch"
    # convention used for `arbiter` above.
    position_review_loop: PositionReviewLoop | None = None
    if arbiter is not None:
        reeval_interval = float(
            os.getenv(
                "CIO_REEVAL_INTERVAL_SECONDS", str(DEFAULT_REEVAL_INTERVAL_SECONDS)
            )
        )
        position_review_loop = PositionReviewLoop(
            runner=arbiter.run_scheduled_review,
            interval_seconds=reeval_interval,
        )
        # Orchestrator registers admitted positions with the loop at
        # admission time (see Orchestrator.run, portfolio_tracker.record_admit
        # call site).
        orchestrator.position_review_loop = position_review_loop
        app.state.position_review_loop = position_review_loop
        logger.info(
            "Position review loop constructed (interval=%.1fs).", reeval_interval
        )
    else:
        logger.info("Position review loop disabled (SIGNAL_ARBITRATION_ENABLED=false).")

    # petrosa_k8s#1130: closes the position lifecycle loop back into CIO.
    # The trade engine echoes position closures on execution.events.>
    # (petrosa_k8s#586); nothing consumed that subject before this, so
    # PortfolioTracker.record_exit and PositionReviewLoop.remove_position
    # were never driven from a real close (ghost positions, #1128/#1129).
    # Wired against the same `orchestrator.portfolio_tracker` singleton the
    # admission path (`Orchestrator.run`) records into, and the same
    # `position_review_loop` instance admission registers positions with —
    # so a real close now retires exactly what admission tracked.
    execution_events_consumer = ExecutionEventsConsumer(
        nats_client=nc,
        portfolio_tracker=orchestrator.portfolio_tracker,
        position_review_loop=position_review_loop,
    )
    app.state.execution_events_consumer = execution_events_consumer

    # #175 (FR66/FR62) — DrawdownBreachEmitter and EnvelopeDriftEmitter are
    # instantiated so they exist as live collaborators on app.state, closing
    # the "never instantiated" half of #175's AC3. The producers that call
    # their `check_and_emit` (a live drawdown-vs-envelope comparator, and a
    # characterization-drift NATS consumer) do not exist yet in this repo
    # and are tracked as follow-up wiring, not silently dropped.
    # (`PortfolioTracker.record_exit` had the same shape of gap — now closed
    # by `ExecutionEventsConsumer` below, petrosa_k8s#1130.)
    drawdown_breach_emitter = DrawdownBreachEmitter(nats_client=nc)
    app.state.drawdown_breach_emitter = drawdown_breach_emitter
    envelope_drift_emitter = EnvelopeDriftEmitter(nats_client=nc)
    app.state.envelope_drift_emitter = envelope_drift_emitter

    # Epic 2: Initialize and Start Heartbeat System (Responder + Publisher)
    heartbeat_subject = os.getenv("NATS_TOPIC_HEARTBEAT", "cio.heartbeat")

    responder = HeartbeatResponder(nats_client=nc, redis_client=redis_client)
    await responder.start(subject=heartbeat_subject)

    publisher = HeartbeatPublisher(nats_client=nc, interval_seconds=10.0)
    await publisher.start(subject=heartbeat_subject)

    # P2.6 (#597): start the evaluator subscriber after NATS is live so
    # arbitration begins consulting the latest upstream verdicts within
    # one tick window of CIO startup.
    await evaluator_subscriber.start()
    logger.info("Evaluator subscriber listening on evaluator.>")

    # petrosa_k8s#810 (AC4.1b): subscribe to alerts.> and forward to Telegram.
    alerts_consumer = AlertsConsumer(nats_client=nc)
    app.state.alerts_consumer = alerts_consumer
    await alerts_consumer.start()
    logger.info("Alerts consumer listening on alerts.>")

    # P7.1 (#610): CIO health evaluator. Subscribes to its own decision
    # audit copies + intent/signal cadence and publishes
    # evaluator.cio.verdict, closing Outcome 5's 8/8 evaluator coverage
    # gate. Started after the upstream subscriber so the loop has fresh
    # state by the time it ticks.
    health_evaluator = CIOHealthEvaluator(nats_client=nc)
    app.state.cio_health_evaluator = health_evaluator
    await health_evaluator.start()
    logger.info("CIO health evaluator publishing on evaluator.cio.verdict")

    # petrosa_k8s#1130: start consuming trade-engine position closures.
    await execution_events_consumer.start()
    logger.info("Execution events consumer listening on execution.events.>")

    # #236: every NATS subscription established above is replayed by
    # `_nats_reconnect_and_resubscribe` after a permanent-closure
    # reconnect. Registered here (after each `.start()` succeeded once)
    # rather than at definition time, so a reconnect never replays a
    # subscription that never actually started.
    _resubscribe_callbacks.extend(
        [
            (
                "heartbeat_responder",
                lambda: responder.start(subject=heartbeat_subject),
            ),
            (
                "heartbeat_publisher",
                lambda: publisher.start(subject=heartbeat_subject),
            ),
            ("evaluator_subscriber", evaluator_subscriber.start),
            ("alerts_consumer", alerts_consumer.start),
            ("health_evaluator", health_evaluator.start),
            ("execution_events_consumer", execution_events_consumer.start),
            ("listener", lambda: listener.start(subject=subscribe_subject)),
        ]
    )
    # Exposed for introspection/tests — mirrors the app.state.nats_client
    # pattern already used for the connection itself.
    app.state.nats_reconnect_state = _nats_reconnect_state

    # #175 (FR60/P1.4-AC7): start the in-position cadence task after
    # everything it depends on (arbiter → context_builder/enforcer/router)
    # is live.
    if position_review_loop is not None:
        await position_review_loop.start()
        logger.info(
            "Position review loop started — open positions will be re-evaluated "
            "on cadence."
        )

    auto_resume_loop = None
    if not auto_resume_enabled():
        logger.info("AUTO_RESUME_DISABLED reason=env_flag")
    elif os.getenv("DRY_RUN", "false").lower() == "true":
        logger.info("AUTO_RESUME_DISABLED reason=dry_run")
    else:
        auto_resume_loop = AutoResumeLoop(
            registry=pause_registry,
            health_tracker=llm_health_tracker,
            llm_client=llm_client,
            http_client=router.http_client,
            service_urls={
                ServiceType.TA_BOT.value: ta_bot_url,
                ServiceType.REALTIME_STRATEGIES.value: realtime_strategies_url,
            },
            cache=cache,
            nats_client=nc,
            interval_seconds=float(os.getenv("CIO_AUTO_RESUME_INTERVAL_SECONDS", "60")),
        )
        app.state.auto_resume_loop = auto_resume_loop
        await auto_resume_loop.start()

    # 3. Graceful Shutdown Setup
    def signal_handler():
        logger.info("Shutdown signal received. Starting graceful exit...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # 4. Start Listening
    await listener.start(subject=subscribe_subject)
    logger.info(f"CIO Strategist is live and listening on {subscribe_subject}")

    # 5. Run Health Check Server in background
    api_port = int(os.getenv("API_PORT", "8000"))
    api_host = os.getenv("API_HOST", "0.0.0.0")  # nosec
    config = uvicorn.Config(app, host=api_host, port=api_port, log_level="warning")  # nosec
    server = uvicorn.Server(config)

    # Run uvicorn in a way that it doesn't block the main event loop entirely
    # or rather, run it as a task.
    asyncio.create_task(server.serve())
    logger.info(f"Health check server started on port {api_port}")

    # Wait for stop signal
    await stop_event.wait()

    # 6. Cleanup Sequence
    logger.info("Cleaning up resources...")
    if position_review_loop is not None:
        await position_review_loop.stop()
    if auto_resume_loop is not None:
        try:
            await auto_resume_loop.stop()
        except Exception as exc:
            logger.warning(f"AUTO_RESUME_STOP_FAILED exc_type={type(exc).__name__}")
    await execution_events_consumer.stop()
    await publisher.stop()
    await responder.stop()
    await health_evaluator.stop()
    await alerts_consumer.stop()
    await evaluator_subscriber.stop()
    await listener.stop()
    await router.close()
    await builder.close()
    await redis_client.close()
    await nc.close()

    # Flush telemetry before exit
    try:
        from petrosa_otel import flush_telemetry

        flush_telemetry()
    except ImportError:
        pass

    await server.shutdown()
    logger.info("CIO Strategist shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
