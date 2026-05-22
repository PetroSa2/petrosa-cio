import logging
from typing import Any

from cio.core.alerting.redundancy import RedundantAlertDispatcher

logger = logging.getLogger(__name__)


class AlertManager:
    """
    Centralized alerting for the Petrosa CIO.
    Dispatches alerts to multiple channels with triple-redundancy.
    """

    _dispatcher = RedundantAlertDispatcher()

    @staticmethod
    async def dispatch_critical_alert(
        message: str, context: dict[str, Any] | None = None
    ):
        """
        Dispatches a CRITICAL (RED) alert to all configured channels.
        """
        ctx = context or {}
        correlation_id = ctx.get("correlation_id", "SYSTEM")

        # Payload construction: Standardized keys win over ctx
        # (Fix for Copilot: merge ctx first, then set standard keys)
        payload = {
            **ctx,
            "alert_type": "RED",
            "correlation_id": correlation_id,
        }

        # 1. Standardized Log Alert (Ingested by Loki/Alloy)
        logger.critical(
            f"CRITICAL_ALERT: {message}",
            extra=payload,
        )

        # 2. Redundant Multi-Channel Dispatch (Grafana API, Otel, Email)
        await AlertManager._dispatcher.dispatch(message, payload)
