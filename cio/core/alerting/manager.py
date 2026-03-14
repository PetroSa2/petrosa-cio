import logging
from typing import Any

logger = logging.getLogger(__name__)


class AlertManager:
    """
    Centralized alerting for the Petrosa CIO.
    Dispatches alerts to multiple channels (primarily Loki/Grafana via logs).
    Triple-redundancy (Email/Otel/NATS) is planned for future phases.
    """

    @staticmethod
    def dispatch_critical_alert(message: str, context: dict[str, Any] | None = None):
        """
        Dispatches a CRITICAL (RED) alert.
        Triggers immediate visibility in Grafana via CRITICAL log level.
        """
        ctx = context or {}
        correlation_id = ctx.get("correlation_id", "SYSTEM")

        # 1. Standardized Log Alert (Ingested by Loki/Alloy)
        logger.critical(
            f"CRITICAL_ALERT: {message}",
            extra={
                "alert_type": "RED",
                "correlation_id": correlation_id,
                **ctx,
            },
        )

        # Future expansion: Prometheus metrics, Email, PagerDuty
        logger.debug(f"Critical alert log emitted for: {message}")
