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
# ARG CACHE_BUST busts the *RUN echo* layer cache key (ARG only affects RUN, not COPY).
# The RUN echo step is a cache miss on every push, which in turn makes the subsequent
# COPY cio/ layer a cache miss because its predecessor layer hash changed.
ARG CACHE_BUST=unknown
RUN echo "Cache bust: $CACHE_BUST"
COPY cio/ cio/
# Build-time integrity guard: abort the build if enforcer.py ships without the call to
# self.orchestrator.run(). Checks for the method call expression specifically (not a comment
# or dead-code string) so substring-in-source false-positives are ruled out.
RUN python3 -c "import sys; sys.path.insert(0, '/app'); from cio.apps.nurse.enforcer import NurseEnforcer; import inspect; src = inspect.getsource(NurseEnforcer.audit); assert 'self.orchestrator.run' in src, 'BUILD FAILED: enforcer.py does not call self.orchestrator.run() — stale cache layer suspected'"

# Create non-root user
RUN useradd -m -u 1000 petrosa && \
    chown -R petrosa:petrosa /app

USER petrosa

# Expose API port (if needed in future, currently NATS only)
EXPOSE 8000

# Run the application
CMD ["python", "-m", "cio.main"]
