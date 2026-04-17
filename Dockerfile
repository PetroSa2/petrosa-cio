# Multi-stage Dockerfile for Petrosa CIO

# Stage 1: Builder
FROM python:3.11-slim as builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and install
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application code
COPY VERSION .
# CACHE_BUST is set to github.sha by CI — forces this layer to rebuild on every push,
# preventing stale cio/ content from being served by the GHA layer cache (see AC1 in #88).
ARG CACHE_BUST=unknown
RUN echo "Cache bust: $CACHE_BUST"
COPY cio/ cio/
# Integrity check: fails the BUILD (not just CI) if the enforcer ships without orchestrator.run().
# This catches stale GHA cache layers before the image is ever pushed.
RUN python3 -c "import sys; sys.path.insert(0, '/app'); from cio.apps.nurse.enforcer import NurseEnforcer; import inspect; src = inspect.getsource(NurseEnforcer.audit); assert 'orchestrator.run' in src, 'BUILD FAILED: enforcer.py does not call orchestrator.run() — stale cache layer suspected'"

# Create non-root user
RUN useradd -m -u 1000 petrosa && \
    chown -R petrosa:petrosa /app

USER petrosa

# Expose API port (if needed in future, currently NATS only)
EXPOSE 8000

# Run the application
CMD ["python", "-m", "cio.main"]
