# Multi-stage Dockerfile for Petrosa CIO

# Stage 1: Builder
FROM python:3.11-slim as builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
    PYTHONUNBUFFERED=1
    PIP_NO_CACHE_DIR=1
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends
    gcc
    g++
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and install
COPY requirements.txt .
RUN pip install --upgrade pip &&
    pip install -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
    PYTHONUNBUFFERED=1
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends
    curl
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application code
COPY main.py .
COPY otel_init.py .
COPY contracts/ contracts/
COPY core/ core/
COPY apps/ apps/

# Create non-root user
RUN useradd -m -u 1000 petrosa &&
    chown -R petrosa:petrosa /app

USER petrosa

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3
    CMD curl -f http://localhost:${API_PORT:-8000}/health/liveness || exit 1

# Expose API port
EXPOSE 8000

# Run the application
CMD ["python", "main.py"]
