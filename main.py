"""
Petrosa CIO - Sovereign Gatekeeper and Strategy Controller
"""

import logging
import os

from fastapi import FastAPI
from nats import connect as nats_connect

from core.nats.heartbeat import HeartbeatService
from otel_init import attach_logging_handler, instrument_fastapi_app, setup_telemetry

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="Petrosa CIO",
    description="Sovereign Gatekeeper and Strategy Controller",
    version="1.0.0",
)
app.state.nats_client = None
app.state.heartbeat_service = None


@app.on_event("startup")
async def startup_event():
    """Run on startup."""
    setup_telemetry(service_name="petrosa-cio")
    instrument_fastapi_app(app)
    attach_logging_handler()
    app.state.heartbeat_service = HeartbeatService(version=app.version)

    nats_url = os.getenv("NATS_URL")
    if nats_url:
        try:
            app.state.nats_client = await nats_connect(nats_url, connect_timeout=1)
            await app.state.heartbeat_service.start(app.state.nats_client)
            logger.info("NATS heartbeat listener active on subject cio.heartbeat")
        except Exception as exc:
            logger.warning(f"NATS heartbeat listener disabled: {exc}")

    logger.info("Petrosa CIO service started")


@app.on_event("shutdown")
async def shutdown_event():
    """Graceful shutdown for optional NATS connection."""
    if app.state.nats_client is not None:
        await app.state.nats_client.close()


@app.get("/health/liveness")
async def liveness():
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/health/readiness")
async def readiness():
    """Readiness probe."""
    return {"status": "ok"}


@app.get("/")
async def root():
    """Root endpoint."""
    return {"service": "petrosa-cio", "version": "1.0.0", "status": "operational"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # nosec
