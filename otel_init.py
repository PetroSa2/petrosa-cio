"""
OpenTelemetry initialization template for Petrosa services.

This template provides a standardized OTEL setup that works for:
- FastAPI/Uvicorn applications (web services)
- Async services (NATS listeners, WebSocket clients)
- CLI/CronJob scripts (batch jobs)

Key lesson from tradeengine investigation:
For FastAPI/Uvicorn applications, OTLP handlers must be attached to BOTH:
1. Root logger (application logs)
2. Uvicorn-specific loggers (server/access/error logs)

This is because uvicorn loggers don't propagate to root logger by design.

Usage:
1. Copy this file to your service as `otel_init.py`
2. For FastAPI/Uvicorn apps: Call `attach_logging_handler()` in lifespan startup
3. For async services: Call `attach_logging_handler_simple()` after setup
4. For CLI jobs: Logging is auto-configured on import
"""

import logging
import os
from typing import Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import (
    OTELResourceDetector,
    ProcessResourceDetector,
    Resource,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Global logger provider for attaching handlers
_global_logger_provider = None
_otlp_logging_handler = None  # Store reference to check if it's still attached

# Initialize logger early (before any telemetry setup)
logger = logging.getLogger(__name__)


def parse_otlp_headers(headers_env: str, signal_type: str) -> dict[str, str] | None:
    """
    Parse OTLP headers from environment variable.

    Args:
        headers_env: Header string in format "key1=value1,key2=value2"
        signal_type: Signal type for logging (e.g., "tracing", "metrics", "logs")

    Returns:
        Dictionary of headers or None if invalid/empty
    """
    if not headers_env or not headers_env.strip():
        return None

    # Parse headers: split by comma, then by =, strip whitespace
    headers_list = [
        tuple(h.strip().split("=", 1))
        for h in headers_env.split(",")
        if "=" in h.strip()
    ]
    headers = {k.strip(): v.strip() for k, v in headers_list}

    # Warn if headers provided but format is invalid
    if not headers:
        logger.warning(
            f"OTEL_EXPORTER_OTLP_HEADERS provided but no valid key=value pairs found. "
            f"Expected format: 'key1=value1,key2=value2'. Got: '{headers_env[:50]}...'"
        )
    else:
        logger.debug(f"Parsed {len(headers)} OTLP header(s) for {signal_type}")

    return headers


# Track initialization state for health checks and metrics
_initialization_state = {
    "tracing": {"success": False, "error": None},
    "metrics": {"success": False, "error": None},
    "logs": {"success": False, "error": None},
    "http_instrumentation": {"success": False, "error": None},
}

# Global meter provider for emitting metrics (set after metrics initialization)
_global_meter_provider = None
_setup_success_gauge = None


def setup_telemetry(
    service_name: str = "petrosa-service",
    service_version: str | None = None,
    otlp_endpoint: str | None = None,
    enable_metrics: bool = True,
    enable_traces: bool = True,
    enable_logs: bool = True,
) -> None:
    """
    Set up OpenTelemetry instrumentation with automatic resource detection.

    Automatically detects and enriches telemetry with:
    - Process attributes (process.pid, process.command, etc.)
    - OTEL attributes (host.name, telemetry.sdk.*, etc.)
    - Kubernetes metadata (when deployed in k8s)
    - Manual attributes (service.name, service.version, deployment.environment)
    - Custom attributes (from OTEL_RESOURCE_ATTRIBUTES env var)

    Args:
        service_name: Name of the service
        service_version: Version of the service
        otlp_endpoint: OTLP endpoint URL
        enable_metrics: Whether to enable metrics
        enable_traces: Whether to enable traces
        enable_logs: Whether to enable logs
    """
    # Early return if OTEL disabled
    if os.getenv("ENABLE_OTEL", "true").lower() not in ("true", "1", "yes"):
        return

    # Get configuration from environment variables
    service_version = service_version or os.getenv("OTEL_SERVICE_VERSION", "1.0.0")
    otlp_endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    enable_metrics = enable_metrics and os.getenv("ENABLE_METRICS", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    enable_traces = enable_traces and os.getenv("ENABLE_TRACES", "true").lower() in (
        "true",
        "1",
        "yes",
    )
    enable_logs = enable_logs and os.getenv("ENABLE_LOGS", "true").lower() in (
        "true",
        "1",
        "yes",
    )

    # Create resource with automatic detection and manual attributes
    # NOTE: service.name and service.version come from OTEL_RESOURCE_ATTRIBUTES env var
    # (set in K8s deployments) to avoid conflicts. Only set attributes here that aren't
    # typically in OTEL_RESOURCE_ATTRIBUTES.
    manual_resource = Resource.create(
        {
            "deployment.environment": os.getenv("ENVIRONMENT", "production"),
            "service.instance.id": os.getenv("HOSTNAME"),
        }
    )

    # Detect process attributes (process.pid, process.command, etc.)
    try:
        process_resource = ProcessResourceDetector().detect()
        logger.debug("Detected process attributes")
    except Exception as e:
        logger.warning(f"Failed to detect process attributes: {e}")
        process_resource = Resource.empty()

    # Detect OTEL environment attributes (host.name, telemetry.sdk.*, etc.)
    try:
        otel_resource = OTELResourceDetector().detect()
        logger.debug("Detected OTEL attributes")
    except Exception as e:
        logger.warning(f"Failed to detect OTEL attributes: {e}")
        otel_resource = Resource.empty()

    # Parse OTEL_RESOURCE_ATTRIBUTES first (single source of truth for service.name/service.version)
    # This takes precedence over manual_resource to avoid conflicts
    custom_attributes = os.getenv("OTEL_RESOURCE_ATTRIBUTES")
    otel_resource_attrs = {}
    if custom_attributes:
        for attr in custom_attributes.split(","):
            if "=" in attr:
                key, value = attr.split("=", 1)
                key = key.strip()
                value = value.strip()
                # Validate: skip empty keys or empty values
                if not key:
                    logger.warning(
                        "Ignoring OTEL resource attribute with empty key from OTEL_RESOURCE_ATTRIBUTES"
                    )
                    continue
                if value == "":
                    logger.warning(
                        f"Ignoring OTEL resource attribute '{key}' with empty value from OTEL_RESOURCE_ATTRIBUTES"
                    )
                    continue
                otel_resource_attrs[key] = value
        if otel_resource_attrs:
            logger.debug(
                f"Parsed {len(otel_resource_attrs)} attributes from OTEL_RESOURCE_ATTRIBUTES: {list(otel_resource_attrs.keys())}"
            )

    # Merge all resources in order of precedence:
    # 1. OTEL_RESOURCE_ATTRIBUTES (highest - single source of truth for service.name/service.version)
    # 2. Manual attributes (deployment.environment, service.instance.id)
    # 3. Detected attributes (process, OTEL)
    # Resource.merge() gives precedence to the caller (left side)
    # Use Resource.empty() if no attributes parsed for consistency
    if otel_resource_attrs:
        base_resource = Resource.create(otel_resource_attrs)
    else:
        base_resource = Resource.empty()

    resource = (
        base_resource.merge(manual_resource)
        .merge(process_resource)
        .merge(otel_resource)
    )

    logger.debug(f"Final resource attributes: {dict(resource.attributes)}")

    # Check for fail-fast option
    fail_fast = os.getenv("OTEL_FAIL_FAST", "false").lower() in ("true", "1", "yes")

    # Set up tracing if enabled
    if enable_traces and otlp_endpoint:
        try:
            tracer_provider = TracerProvider(resource=resource)

            # Parse OTLP headers
            headers_env = os.getenv("OTEL_EXPORTER_OTLP_HEADERS")
            span_headers = (
                parse_otlp_headers(headers_env, "tracing") if headers_env else None
            )

            otlp_exporter = OTLPSpanExporter(
                endpoint=otlp_endpoint,
                headers=span_headers,
            )

            tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
            trace.set_tracer_provider(tracer_provider)

            _initialization_state["tracing"]["success"] = True
            logger.info(f"OpenTelemetry tracing enabled for {service_name}")

        except Exception as e:
            _initialization_state["tracing"]["error"] = str(e)
            logger.error(
                f"Failed to set up OpenTelemetry tracing: {e}",
                exc_info=True,
                extra={"service": service_name, "component": "tracing"},
            )
            if fail_fast:
                raise

    # Set up metrics if enabled
    if enable_metrics and otlp_endpoint:
        global _global_meter_provider, _setup_success_gauge
        try:
            headers_env = os.getenv("OTEL_EXPORTER_OTLP_HEADERS")
            metric_headers = (
                parse_otlp_headers(headers_env, "metrics") if headers_env else None
            )

            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=otlp_endpoint,
                    headers=metric_headers,
                ),
                export_interval_millis=int(
                    os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "60000")
                ),
            )

            meter_provider = MeterProvider(
                resource=resource, metric_readers=[metric_reader]
            )
            metrics.set_meter_provider(meter_provider)
            _global_meter_provider = meter_provider

            # Create gauge metric for initialization success
            meter = metrics.get_meter(__name__)
            _setup_success_gauge = meter.create_gauge(
                name="otel_setup_success",
                description="OpenTelemetry initialization success (1=success, 0=failure)",
                unit="1",
            )

            _initialization_state["metrics"]["success"] = True
            if _setup_success_gauge:
                _setup_success_gauge.set(1, {"component": "metrics"})
            logger.info(f"OpenTelemetry metrics enabled for {service_name}")

        except Exception as e:
            _initialization_state["metrics"]["error"] = str(e)
            logger.error(
                f"Failed to set up OpenTelemetry metrics: {e}",
                exc_info=True,
                extra={"service": service_name, "component": "metrics"},
            )
            # Note: Cannot emit failure metric here because metrics initialization failed
            # The failure is logged above and tracked in _initialization_state
            if fail_fast:
                raise

    # Set up logging export via OTLP if enabled
    if enable_logs and otlp_endpoint:
        global _global_logger_provider
        try:
            # Enrich logs with trace context
            # set_logging_format=False to avoid clearing existing handlers
            LoggingInstrumentor().instrument(set_logging_format=False)

            # Parse log headers
            headers_env = os.getenv("OTEL_EXPORTER_OTLP_HEADERS")
            log_headers = (
                parse_otlp_headers(headers_env, "logs") if headers_env else None
            )

            log_exporter = OTLPLogExporter(
                endpoint=otlp_endpoint,
                headers=log_headers,
            )

            logger_provider = LoggerProvider(resource=resource)
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(log_exporter)
            )

            # Store globally for later attachment
            _global_logger_provider = logger_provider

            _initialization_state["logs"]["success"] = True
            logger.info(f"OpenTelemetry logging export configured for {service_name}")
            logger.info(
                "Note: Call attach_logging_handler() after app starts to activate"
            )
            if _setup_success_gauge:
                _setup_success_gauge.set(1, {"component": "logs"})

        except Exception as e:
            _initialization_state["logs"]["error"] = str(e)
            logger.error(
                f"Failed to set up OpenTelemetry logging export: {e}",
                exc_info=True,
                extra={"service": service_name, "component": "logs"},
            )
            if _setup_success_gauge:
                _setup_success_gauge.set(0, {"component": "logs"})
            if fail_fast:
                raise

    # Set up HTTP instrumentation
    try:
        RequestsInstrumentor().instrument()
        URLLib3Instrumentor().instrument()
        _initialization_state["http_instrumentation"]["success"] = True
        logger.info(f"OpenTelemetry HTTP instrumentation enabled for {service_name}")
        if _setup_success_gauge:
            _setup_success_gauge.set(1, {"component": "http_instrumentation"})

    except Exception as e:
        _initialization_state["http_instrumentation"]["error"] = str(e)
        logger.error(
            f"Failed to set up OpenTelemetry HTTP instrumentation: {e}",
            exc_info=True,
            extra={"service": service_name, "component": "http_instrumentation"},
        )
        if _setup_success_gauge:
            _setup_success_gauge.set(0, {"component": "http_instrumentation"})
        if fail_fast:
            raise

    # Log summary
    failed_components = [
        k for k, v in _initialization_state.items() if v.get("error") is not None
    ]

    if failed_components:
        logger.warning(
            f"OpenTelemetry setup completed for {service_name} v{service_version} "
            f"with {len(failed_components)} failed component(s): {', '.join(failed_components)}"
        )
    else:
        logger.info(
            f"OpenTelemetry setup completed successfully for {service_name} v{service_version}"
        )


def instrument_fastapi_app(app, fail_fast: bool | None = None):
    """
    Instrument a FastAPI application.

    Call this after creating your FastAPI app instance.

    Args:
        app: FastAPI application instance
        fail_fast: Whether to fail fast on errors (defaults to OTEL_FAIL_FAST env var)
    """
    # Use provided fail_fast or read from environment (consistent with setup_telemetry)
    if fail_fast is None:
        fail_fast = os.getenv("OTEL_FAIL_FAST", "false").lower() in ("true", "1", "yes")

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        logger.info("FastAPI application instrumented")
    except Exception as e:
        logger.error(
            f"Failed to instrument FastAPI application: {e}",
            exc_info=True,
        )
        if fail_fast:
            raise


def attach_logging_handler():
    """
    Attach OTLP logging handler to root logger and uvicorn loggers.

    ⚠️  FOR FASTAPI/UVICORN APPLICATIONS ONLY ⚠️

    This should be called AFTER uvicorn/FastAPI configures logging,
    typically in the lifespan startup function.

    We attach to both root logger AND uvicorn-specific loggers because:
    1. Root logger captures application logs
    2. Uvicorn loggers (uvicorn, uvicorn.access, uvicorn.error) bypass root logger
       and need explicit handler attachment to capture server/access logs

    For non-Uvicorn services, use attach_logging_handler_simple() instead.
    """
    global _global_logger_provider, _otlp_logging_handler

    if _global_logger_provider is None:
        logger.warning("Logger provider not configured - logging export not available")
        return False

    try:
        # Get loggers
        root_logger = logging.getLogger()
        uvicorn_logger = logging.getLogger("uvicorn")
        uvicorn_access_logger = logging.getLogger("uvicorn.access")
        uvicorn_error_logger = logging.getLogger("uvicorn.error")

        # Check if handler already attached
        if _otlp_logging_handler is not None:
            if _otlp_logging_handler in root_logger.handlers:
                logger.debug("OTLP logging handler already attached")
                return True
            else:
                logger.warning("OTLP handler was removed, re-attaching...")

        # Create new handler
        handler = LoggingHandler(
            level=logging.NOTSET,
            logger_provider=_global_logger_provider,
        )

        # Attach to root logger
        root_logger.addHandler(handler)

        # Also attach to uvicorn loggers
        uvicorn_logger.addHandler(handler)
        uvicorn_access_logger.addHandler(handler)
        uvicorn_error_logger.addHandler(handler)

        _otlp_logging_handler = handler

        logger.info("OTLP logging handler attached to root and uvicorn loggers")
        logger.debug(
            f"Root logger handlers: {len(root_logger.handlers)}, "
            f"Uvicorn logger handlers: {len(uvicorn_logger.handlers)}, "
            f"Uvicorn access logger handlers: {len(uvicorn_access_logger.handlers)}"
        )

        return True

    except Exception as e:
        logger.error(f"Failed to attach logging handler: {e}", exc_info=True)
        return False


def attach_logging_handler_simple():
    """
    Attach OTLP logging handler to root logger only.

    ⚠️  FOR NON-UVICORN SERVICES ⚠️

    Use this for:
    - Async services (NATS listeners, WebSocket clients)
    - CLI/CronJob scripts
    - Any service that doesn't use Uvicorn

    For FastAPI/Uvicorn services, use attach_logging_handler() instead.
    """
    global _global_logger_provider, _otlp_logging_handler

    if _global_logger_provider is None:
        logger.warning("Logger provider not configured - logging export not available")
        return False

    try:
        root_logger = logging.getLogger()

        # Check if handler already attached
        if _otlp_logging_handler is not None:
            if _otlp_logging_handler in root_logger.handlers:
                logger.debug("OTLP logging handler already attached")
                return True
            else:
                logger.warning("OTLP handler was removed, re-attaching...")

        # Create and attach handler
        handler = LoggingHandler(
            level=logging.NOTSET,
            logger_provider=_global_logger_provider,
        )

        root_logger.addHandler(handler)
        _otlp_logging_handler = handler

        logger.info("OTLP logging handler attached to root logger")
        logger.debug(f"Root logger handlers: {len(root_logger.handlers)}")

        return True

    except Exception as e:
        logger.error(f"Failed to attach logging handler: {e}", exc_info=True)
        return False


def monitor_logging_handlers():
    """
    Monitor and re-attach OTLP logging handler if needed.

    Checks root logger and uvicorn loggers (if applicable).
    Call this periodically in a watchdog for extra safety.
    """
    global _otlp_logging_handler

    if _global_logger_provider is None:
        return False

    root_logger = logging.getLogger()

    # Check if handler is missing from root logger
    if _otlp_logging_handler not in root_logger.handlers:
        logger.warning("OTLP handler missing from root logger - re-attaching")
        # Try to re-attach with uvicorn support first, fallback to simple
        try:
            return attach_logging_handler()
        except Exception:
            return attach_logging_handler_simple()

    return True


def get_tracer(name: str = None) -> trace.Tracer:
    """
    Get a tracer instance.

    Args:
        name: Tracer name

    Returns:
        Tracer instance
    """
    return trace.get_tracer(name or "petrosa-service")


def get_meter(name: str = None) -> metrics.Meter:
    """
    Get a meter instance.

    Args:
        name: Meter name

    Returns:
        Meter instance
    """
    return metrics.get_meter(name or "petrosa-service")


def check_otlp_health() -> dict:
    """
    Check OTLP connectivity and initialization health.

    Returns:
        Dictionary with health status for each component:
        {
            "healthy": bool,
            "tracing": {"enabled": bool, "status": "ok" | "failed" | "not_configured"},
            "metrics": {"enabled": bool, "status": "ok" | "failed" | "not_configured"},
            "logs": {"enabled": bool, "status": "ok" | "failed" | "not_configured"},
            "http_instrumentation": {"enabled": bool, "status": "ok" | "failed" | "not_configured"},
        }
    """
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    enable_traces = os.getenv("ENABLE_TRACES", "true").lower() in ("true", "1", "yes")
    enable_metrics = os.getenv("ENABLE_METRICS", "true").lower() in ("true", "1", "yes")
    enable_logs = os.getenv("ENABLE_LOGS", "true").lower() in ("true", "1", "yes")

    health = {
        "healthy": True,
        "tracing": {
            "enabled": enable_traces and otlp_endpoint is not None,
            "status": "not_configured",
        },
        "metrics": {
            "enabled": enable_metrics and otlp_endpoint is not None,
            "status": "not_configured",
        },
        "logs": {
            "enabled": enable_logs and otlp_endpoint is not None,
            "status": "not_configured",
        },
        "http_instrumentation": {
            "enabled": True,
            "status": "not_configured",
        },
    }

    # Check tracing
    if health["tracing"]["enabled"]:
        if _initialization_state["tracing"]["success"]:
            health["tracing"]["status"] = "ok"
        elif _initialization_state["tracing"]["error"]:
            health["tracing"]["status"] = "failed"
            health["healthy"] = False
            health["tracing"]["error"] = _initialization_state["tracing"]["error"]
        else:
            # Enabled but never attempted initialization (shouldn't happen, but handle it)
            health["tracing"]["status"] = "unknown"
            health["healthy"] = False
            health["tracing"][
                "error"
            ] = "Initialization was expected but never attempted"

    # Check metrics
    if health["metrics"]["enabled"]:
        if _initialization_state["metrics"]["success"]:
            health["metrics"]["status"] = "ok"
        elif _initialization_state["metrics"]["error"]:
            health["metrics"]["status"] = "failed"
            health["healthy"] = False
            health["metrics"]["error"] = _initialization_state["metrics"]["error"]
        else:
            # Enabled but never attempted initialization
            health["metrics"]["status"] = "unknown"
            health["healthy"] = False
            health["metrics"][
                "error"
            ] = "Initialization was expected but never attempted"

    # Check logs
    if health["logs"]["enabled"]:
        if _initialization_state["logs"]["success"]:
            health["logs"]["status"] = "ok"
        elif _initialization_state["logs"]["error"]:
            health["logs"]["status"] = "failed"
            health["healthy"] = False
            health["logs"]["error"] = _initialization_state["logs"]["error"]
        else:
            # Enabled but never attempted initialization
            health["logs"]["status"] = "unknown"
            health["healthy"] = False
            health["logs"]["error"] = "Initialization was expected but never attempted"

    # Check HTTP instrumentation
    if health["http_instrumentation"]["enabled"]:
        if _initialization_state["http_instrumentation"]["success"]:
            health["http_instrumentation"]["status"] = "ok"
        elif _initialization_state["http_instrumentation"]["error"]:
            health["http_instrumentation"]["status"] = "failed"
            health["healthy"] = False
            health["http_instrumentation"]["error"] = _initialization_state[
                "http_instrumentation"
            ]["error"]
        else:
            # Enabled but never attempted initialization
            health["http_instrumentation"]["status"] = "unknown"
            health["healthy"] = False
            health["http_instrumentation"][
                "error"
            ] = "Initialization was expected but never attempted"

    return health


def get_initialization_state() -> dict:
    """
    Get the current initialization state for debugging.

    Returns:
        Dictionary with initialization state for each component
    """
    return _initialization_state.copy()


# Auto-setup if environment variable is set and not disabled
if os.getenv("ENABLE_OTEL", "true").lower() in ("true", "1", "yes"):
    if not os.getenv("OTEL_NO_AUTO_INIT"):
        if os.getenv("OTEL_AUTO_SETUP", "true").lower() in ("true", "1", "yes"):
            setup_telemetry()
