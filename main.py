"""
Petrosa CIO - Sovereign Gatekeeper and Strategy Controller
"""

import logging
import os
import asyncio

from fastapi import FastAPI
from nats import connect as nats_connect

from apps.nurse.roi_engine import ShadowROIEngine
from apps.nurse.roi_logger import RoiLogger
from core.nats.heartbeat import HeartbeatService
from core.nats.interceptor import NurseInterceptor
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
app.state.roi_engine = None
app.state.interceptor = None


@app.on_event("startup")
async def startup_event():
    """Run on startup."""
    setup_telemetry(service_name="petrosa-cio")
    instrument_fastapi_app(app)
    attach_logging_handler()
    app.state.heartbeat_service = HeartbeatService(version=app.version)
    app.state.roi_engine = ShadowROIEngine()
    await app.state.roi_engine.start()

    nats_url = os.getenv("NATS_URL", "nats://petrosa-nats:4222")
    if nats_url:
        try:
            app.state.nats_client = await nats_connect(
                nats_url, 
                connect_timeout=5,
                reconnect_time_wait=1,
                max_reconnect_attempts=10
            )
            
            # Start Heartbeat
            await app.state.heartbeat_service.start(app.state.nats_client)
            logger.info("NATS heartbeat listener active on subject cio.heartbeat")
            
            # Start Interceptor with correct ROI logger
            roi_logger = RoiLogger() # Default uses None collection for now
            app.state.interceptor = NurseInterceptor(
                nats_client=app.state.nats_client,
                roi_logger=roi_logger,
                target_subject=os.getenv("NATS_TOPIC_SIGNALS", "signals.trading")
            )
            await app.state.interceptor.start()
            logger.info(f"CIO Interceptor active on subject cio.intent.>")
            
        except Exception as exc:
            logger.error(f"❌ Failed to initialize NATS components: {exc}", exc_info=True)

    logger.info("Petrosa CIO service started")


@app.on_event("shutdown")
async def shutdown_event():
    """Graceful shutdown for optional NATS connection."""
    if app.state.nats_client is not None:
        await app.state.nats_client.close()
    if app.state.roi_engine is not None:
        await app.state.roi_engine.stop()


@app.get("/health/liveness")
async def liveness():
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/health/readiness")
async def readiness():
    """Readiness probe."""
    if app.state.nats_client and app.state.nats_client.is_connected:
        return {"status": "ok"}
    return {"status": "degraded", "nats": "disconnected"}


@app.get("/")
async def root():
    """Root endpoint."""
    return {"service": "petrosa-cio", "version": "1.0.0", "status": "operational"}


@app.get("/api/governance/earnings-summary")
async def get_earnings_summary(window_hours: int = 24 * 7):
    """Return governance earnings summary (Actual PnL vs Shadow ROI)."""
    if app.state.roi_engine is None:
        app.state.roi_engine = ShadowROIEngine()
    return await app.state.roi_engine.get_earnings_summary(window_hours=window_hours)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # nosec
