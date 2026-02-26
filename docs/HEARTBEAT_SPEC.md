# CIO Heartbeat Spec

## Subject
- `cio.heartbeat` (NATS request-reply)

## Objective
Provide deterministic governance liveness with dependency connectivity checks so clients can decide whether governance is active.

## Response Schema
```json
{
  "status": "OK",
  "status_code": "GOVERNANCE_ACTIVE | DEGRADED",
  "timestamp": "2026-02-26T03:00:00+00:00",
  "version": "1.0.0",
  "dependencies": {
    "redis": "connected | disconnected",
    "mongo": "connected | disconnected"
  },
  "response_time_ms": 1.23
}
```

## Status Codes
- `GOVERNANCE_ACTIVE`: Redis and MongoDB connectivity checks succeeded.
- `DEGRADED`: At least one dependency health check failed.

## Latency Contract
- Target response budget: `< 20ms` from receipt to reply.
- The heartbeat handler records span attributes for status and timing.

## Telemetry
Heartbeat execution is wrapped in an OpenTelemetry span: `cio.heartbeat`.

Span attributes:
- `service.health.status`
- `service.health.status_code`
- `service.health.redis`
- `service.health.mongo`
- `service.health.response_time_ms`
- `service.health.under_budget`
