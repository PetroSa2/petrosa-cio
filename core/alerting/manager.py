"""Triple-redundant alerting manager and channels."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from enum import StrEnum
from typing import Any

from prometheus_client import CollectorRegistry, Counter

from otel_init import get_tracer


class AlertSeverity(StrEnum):
    """Alert severities for governance and system failures."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(slots=True)
class AlertEvent:
    """Canonical alert payload sent to all channels."""

    source: str
    category: str
    message: str
    severity: AlertSeverity
    symbol: str | None = None
    count: int = 1
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class AlertChannel:
    """Channel interface."""

    async def send(self, event: AlertEvent) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class OtelChannel(AlertChannel):
    """OpenTelemetry span-based alert channel."""

    def __init__(self, tracer_name: str = "core.alerting.manager"):
        self.tracer = get_tracer(tracer_name)

    async def send(self, event: AlertEvent) -> None:
        with self.tracer.start_as_current_span("cio.alert.dispatch") as span:
            span.set_attribute("alert.source", event.source)
            span.set_attribute("alert.category", event.category)
            span.set_attribute("alert.severity", event.severity)
            span.set_attribute("alert.message", event.message)
            span.set_attribute("alert.count", event.count)
            if event.symbol is not None:
                span.set_attribute("alert.symbol", event.symbol)
            if event.context.get("trace_id"):
                span.set_attribute("alert.trace_id", str(event.context["trace_id"]))
            if event.context.get("reasoning_trace"):
                span.set_attribute(
                    "alert.reasoning_trace", str(event.context["reasoning_trace"])
                )
            if event.context.get("audit_log_url"):
                span.set_attribute(
                    "alert.audit_log_url", str(event.context["audit_log_url"])
                )


class GrafanaChannel(AlertChannel):
    """Prometheus metric counter channel for Grafana alerting."""

    def __init__(self, registry: CollectorRegistry | None = None):
        self.counter = Counter(
            "cio_alert_events_total",
            "Total alert events emitted by CIO alert manager",
            ["severity", "category", "source"],
            registry=registry,
        )

    async def send(self, event: AlertEvent) -> None:
        self.counter.labels(
            severity=event.severity,
            category=event.category,
            source=event.source,
        ).inc(event.count)


class EmailChannel(AlertChannel):
    """SMTP channel for critical/human-facing alert messages."""

    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        sender: str,
        recipients: list[str],
        smtp_factory: Any = smtplib.SMTP,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender = sender
        self.recipients = recipients
        self.smtp_factory = smtp_factory

    async def send(self, event: AlertEvent) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[{event.severity}] {event.category}"
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        body = [
            f"message: {event.message}",
            f"source: {event.source}",
            f"severity: {event.severity}",
            f"trace_id: {event.context.get('trace_id', '')}",
            f"reasoning_trace: {event.context.get('reasoning_trace', '')}",
            f"audit_log_url: {event.context.get('audit_log_url', '')}",
            f"count: {event.count}",
        ]
        msg.set_content("\n".join(body))

        with self.smtp_factory(self.smtp_host, self.smtp_port) as smtp:
            smtp.send_message(msg)


class AlertManager:
    """Dispatches alerts to all configured channels."""

    def __init__(self, channels: list[AlertChannel]):
        self.channels = channels

    async def dispatch(self, event: AlertEvent) -> None:
        for channel in self.channels:
            await channel.send(event)
