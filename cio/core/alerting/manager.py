import logging
from typing import Any

logger = logging.getLogger(__name__)


class AlertManager:
    """
    Centralized alerting for the Petrosa CIO.
    Dispatches alerts to multiple channels (Grafana/Loki, Otel, Email).
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
            f"🚨 CRITICAL_ALERT: {message}",
            extra={
                "alert_type": "RED",
                "correlation_id": correlation_id,
                **ctx,
            },
        )

        # 2. Prometheus Metric Increment (Optional but good for alerting rules)
        # from cio.core.metrics import CRITICAL_ALERTS
        # CRITICAL_ALERTS.inc()

        # 3. Future expansion: Email, PagerDuty, NATS emergency.publish
        logger.info(f"Critical alert dispatched to monitoring stack: {message}")
