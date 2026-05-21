import asyncio
import logging
import os
import signal
import sys

import uvicorn
from fastapi import FastAPI
from nats.aio.client import Client as NATS

from cio.apps.authority_api import router as authority_router
from cio.apps.lifecycle_api import router as lifecycle_router
from cio.apps.nurse.enforcer import NurseEnforcer
from cio.apps.state_api import router as state_router
from cio.clients.factory import ClientFactory
from cio.core.arbiter import SignalArbiter
from cio.core.authority import AuthorityStore
from cio.core.cache import AsyncRedisCache
from cio.core.context_builder import ContextBuilder
from cio.core.evaluator_subscriber import EvaluatorSubscriber
from cio.core.heartbeat import HeartbeatPublisher, HeartbeatResponder
from cio.core.lifecycle import StrategyLifecycleStore
from cio.core.listener import NATSListener
from cio.core.orchestrator import Orchestrator
from cio.core.router import OutputRouter

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

logger = logging.getLogger("cio-strategist")

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


@app.get("/health/liveness")
async def liveness():
    return {"status": "ok"}


@app.get("/health/readiness")
async def readiness():
    # Basic check for NATS connection
    if hasattr(app.state, "nats_client") and app.state.nats_client.is_connected:
        return {"status": "ok"}
    return {"status": "degraded", "nats": "disconnected"}


async def main():
    # 0. Start Metrics Server (Epic 5)
    import prometheus_client

    prometheus_port = int(os.getenv("METRICS_PORT", "9090"))
    prometheus_client.start_http_server(prometheus_port)
    logger.info(f"Prometheus metrics server started on port {prometheus_port}")

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
            success = attach_logging_handler()
            if success:
                logger.info(
                    "✅ OpenTelemetry logging handler attached - logs will be exported to Grafana"
                )
        except Exception as e:
            logger.error(f"Failed to attach OTel logging handler: {e}")

    # 1. Load Configuration
    nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
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
    nc = NATS()
    try:
        await nc.connect(nats_url)
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

    # 3. Graceful Shutdown Setup
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received. Starting graceful exit...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # 4. Start Listening
    intents_subject = os.getenv("NATS_TOPIC_INTENTS", "cio.intent.trading")
    # AC: Use multi-token wildcard '>' to capture all strategy-specific intents
    # following the Petrosa NATS contract.
    if not intents_subject.endswith(">"):
        base_subject = intents_subject.rstrip(".*")
        subscribe_subject = f"{base_subject}.>"
    else:
        subscribe_subject = intents_subject

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
    await publisher.stop()
    await responder.stop()
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
