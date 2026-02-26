"""Tests for alert manager channels and distillation."""

import pytest
from prometheus_client import CollectorRegistry

from apps.nurse.alert_distiller import AlertDistiller
from core.alerting.manager import (
    AlertEvent,
    AlertManager,
    AlertSeverity,
    EmailChannel,
    GrafanaChannel,
    OtelChannel,
)
from core.monitoring.heartbeat_watcher import HeartbeatWatcher


class FakeSMTP:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sent_messages = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        _ = exc_type, exc, tb
        return False

    def send_message(self, msg):
        self.sent_messages.append(msg)


class SMTPFactory:
    def __init__(self):
        self.instances = []

    def __call__(self, host: str, port: int):
        instance = FakeSMTP(host, port)
        self.instances.append(instance)
        return instance


class RecordingChannel:
    def __init__(self):
        self.events = []

    async def send(self, event: AlertEvent):
        self.events.append(event)


@pytest.mark.asyncio
async def test_multi_channel_dispatch_includes_context_fields():
    smtp_factory = SMTPFactory()
    registry = CollectorRegistry()

    otel_channel = OtelChannel()
    grafana_channel = GrafanaChannel(registry=registry)
    email_channel = EmailChannel(
        smtp_host="localhost",
        smtp_port=25,
        sender="cio@example.com",
        recipients=["ops@example.com"],
        smtp_factory=smtp_factory,
    )

    manager = AlertManager([otel_channel, grafana_channel, email_channel])
    event = AlertEvent(
        source="nurse",
        category="semantic_veto",
        message="Trade vetoed by regime guard",
        severity=AlertSeverity.WARNING,
        symbol="BTCUSDT",
        context={
            "trace_id": "abc123",
            "reasoning_trace": "Detailed reasoning trace from strategist",
            "audit_log_url": "https://audit.local/log/1",
        },
    )

    await manager.dispatch(event)

    metric = grafana_channel.counter.labels(
        severity=event.severity,
        category=event.category,
        source=event.source,
    )
    assert metric._value.get() == pytest.approx(1.0)  # noqa: SLF001

    assert len(smtp_factory.instances) == 1
    sent = smtp_factory.instances[0].sent_messages[0]
    body = sent.get_content()
    assert "abc123" in body
    assert "Detailed reasoning trace" in body
    assert "https://audit.local/log/1" in body


@pytest.mark.asyncio
async def test_distiller_groups_similar_events_into_summary_alert():
    distiller = AlertDistiller(aggregation_window_seconds=300)

    for _ in range(50):
        distiller.ingest(
            AlertEvent(
                source="nurse",
                category="semantic_veto",
                message="Trade vetoed by regime guard",
                severity=AlertSeverity.WARNING,
                symbol="BTCUSDT",
                context={
                    "trace_id": "tid-1",
                    "reasoning_trace": "trace",
                    "audit_log_url": "https://audit.local/log/2",
                },
            ),
            now=1000.0,
        )

    emitted = distiller.flush(now=1301.0)

    assert len(emitted) == 1
    summary = emitted[0]
    assert summary.count == 50
    assert "Summary Alert" in summary.message
    assert summary.context["trace_id"] == "tid-1"
    assert summary.context["audit_log_url"] == "https://audit.local/log/2"


@pytest.mark.asyncio
async def test_heartbeat_watcher_triggers_critical_alert_after_timeout():
    recording = RecordingChannel()
    manager = AlertManager([recording])

    now = 1000.0

    def clock():
        return now

    watcher = HeartbeatWatcher(
        alert_manager=manager,
        stale_after_seconds=60,
        clock=clock,
    )

    watcher.record_heartbeat()

    assert await watcher.check_health() is True

    now = 1065.0
    assert await watcher.check_health() is False
    assert len(recording.events) == 1
    assert recording.events[0].severity == AlertSeverity.CRITICAL
    assert "Heartbeat stale" in recording.events[0].message
