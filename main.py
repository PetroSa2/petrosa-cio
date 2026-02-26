"""
Petrosa CIO - Sovereign Gatekeeper and Strategy Controller
"""

import logging

from fastapi import FastAPI

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


@app.on_event("startup")
async def startup_event():
    """Run on startup."""
    setup_telemetry(service_name="petrosa-cio")
    instrument_fastapi_app(app)
    attach_logging_handler()
    logger.info("Petrosa CIO service started")


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
