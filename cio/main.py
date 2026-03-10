import asyncio
import logging
import os
import signal
import sys

import uvicorn
from fastapi import FastAPI
from nats.aio.client import Client as NATS

from cio.clients.factory import ClientFactory
from cio.core.cache import AsyncRedisCache
from cio.core.context_builder import ContextBuilder
from cio.core.listener import NATSListener
from cio.core.orchestrator import Orchestrator
from cio.core.router import OutputRouter


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
    router = OutputRouter(
        nats_client=nc,
        vector_client=vector_client,
        ta_bot_url=ta_bot_url,
        realtime_strategies_url=realtime_strategies_url,
        cache=cache,
    )

    listener = NATSListener(
        nats_client=nc,
        orchestrator=orchestrator,
        context_builder=builder,
        router=router,
    )

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
    await listener.start(subject=intents_subject)
    logger.info(f"CIO Strategist is live and listening on {intents_subject}")

    # 5. Run Health Check Server in background
    api_port = int(os.getenv("API_PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=api_port, log_level="warning")
    server = uvicorn.Server(config)

    # Run uvicorn in a way that it doesn't block the main event loop entirely
    # or rather, run it as a task.
    asyncio.create_task(server.serve())
    logger.info(f"Health check server started on port {api_port}")

    # Wait for stop signal
    await stop_event.wait()

    # 6. Cleanup Sequence
    logger.info("Cleaning up resources...")
    await listener.stop()
    await router.close()
    await builder.close()
    await redis_client.close()
    await nc.close()
    await server.shutdown()
    logger.info("CIO Strategist shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
