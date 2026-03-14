import asyncio
import logging
import os
import smtplib
from abc import ABC, abstractmethod
from email.mime.text import MIMEText
from typing import Any

import httpx
from opentelemetry import trace

logger = logging.getLogger(__name__)

class AlertChannel(ABC):
    @abstractmethod
    async def send(self, message: str, context: dict[str, Any]) -> bool:
        pass

class GrafanaChannel(AlertChannel):
    """
    Sends alerts to Grafana via HTTP API (e.g., Annotations or specialized endpoint).
    Also relies on Loki logs as a secondary path.
    """
    def __init__(self):
        self.api_url = os.getenv("GRAFANA_API_URL")
        self.api_key = os.getenv("GRAFANA_API_KEY")

    async def send(self, message: str, context: dict[str, Any]) -> bool:
        # 1. Primary path: HTTP API for Annotations (visibility in dashboards)
        if self.api_url and self.api_key:
            try:
                async with httpx.AsyncClient() as client:
                    payload = {
                        "text": message,
                        "tags": ["alert", "cio", context.get("alert_type", "RED")],
                        "time": int(context.get("timestamp", 0) * 1000) or None
                    }
                    response = await client.post(
                        f"{self.api_url}/api/annotations",
                        json=payload,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        timeout=5.0
                    )
                    if response.status_code not in (200, 201):
                        logger.error(f"Grafana API alert failed: {response.text}")
            except Exception as e:
                logger.error(f"Error sending alert to Grafana API: {e}")

        # 2. Secondary path: High-visibility logs (picked up by Loki)
        logger.critical(f"GRAFANA_ALERT: {message}", extra=context)
        return True

class OtelChannel(AlertChannel):
    """
    Exports alerts as OpenTelemetry Error Spans.
    """
    def __init__(self):
        self.tracer = trace.get_tracer("cio.alerting")

    async def send(self, message: str, context: dict[str, Any]) -> bool:
        with self.tracer.start_as_current_span("CRITICAL_ALERT") as span:
            span.set_attribute("alert.message", message)
            span.set_attribute("alert.type", context.get("alert_type", "RED"))
            span.set_status(trace.Status(trace.StatusCode.ERROR, message))
            for key, value in context.items():
                span.set_attribute(f"alert.context.{key}", str(value))
        return True

class EmailChannel(AlertChannel):
    """
    Sends alerts via SMTP.
    """
    def __init__(self):
        self.smtp_host = os.getenv("SMTP_HOST", "localhost")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER")
        self.smtp_pass = os.getenv("SMTP_PASS")
        self.from_email = os.getenv("ALERT_EMAIL_FROM", "alerts@petrosa.com")
        self.to_email = os.getenv("ALERT_EMAIL_TO", "admin@petrosa.com")

    def _send_sync(self, msg: MIMEText):
        """Synchronous SMTP send to be run in a thread."""
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            server.starttls()
            server.login(self.smtp_user, self.smtp_pass)
            server.send_message(msg)

    async def send(self, message: str, context: dict[str, Any]) -> bool:
        if not self.smtp_user or not self.smtp_pass:
            logger.warning("SMTP credentials not configured, skipping email alert.")
            return False

        try:
            msg = MIMEText(f"CRITICAL ALERT FROM CIO\n\nMessage: {message}\n\nContext: {context}")
            msg["Subject"] = f"[CIO ALERT] {context.get('alert_type', 'RED')}: {message[:50]}..."
            msg["From"] = self.from_email
            msg["To"] = self.to_email

            # Use asyncio.to_thread to avoid blocking the event loop (Fix for Copilot)
            await asyncio.to_thread(self._send_sync, msg)
            
            logger.info("Email alert sent successfully.")
            return True
        except Exception as e:
            logger.error(f"Failed to send email alert: {e}")
            return False
