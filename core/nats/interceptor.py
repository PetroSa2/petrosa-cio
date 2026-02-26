"""NATS intent interception and promotion to trading signals."""

import json
import time
from typing import Any

from apps.nurse.enforcer import EnforcerResult, NurseEnforcer
from apps.nurse.roi_logger import RoiLogger


class NurseInterceptor:
    """Intercepts `cio.intent.>` events and promotes approved payloads."""

    def __init__(
        self,
        nats_client: Any,
        enforcer: NurseEnforcer | None = None,
        roi_logger: RoiLogger | None = None,
        target_subject: str = "signals.trading",
        max_latency_ms: float = 50.0,
    ):
        self.nats_client = nats_client
        self.enforcer = enforcer or NurseEnforcer()
        self.roi_logger = roi_logger or RoiLogger()
        self.target_subject = target_subject
        self.max_latency_ms = max_latency_ms

    async def start(self) -> None:
        """Start subscription loop for all CIO intents."""
        await self.nats_client.subscribe("cio.intent.>", cb=self._on_message)

    async def _on_message(self, msg: Any) -> None:
        headers = self._normalize_headers(getattr(msg, "headers", None))
        await self.handle_intent(msg.data, headers=headers)

    async def handle_intent(
        self,
        data: bytes | str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        start = time.perf_counter()
        payload = self._decode_payload(data)
        traceparent = self._extract_traceparent(headers)

        result: EnforcerResult = await self.enforcer.enforce(payload)

        promoted_payload = dict(payload)
        if result.metadata and "scaled_quantity" in result.metadata:
            promoted_payload["quantity"] = result.metadata["scaled_quantity"]
        if traceparent:
            promoted_payload["_otel_trace_context"] = {"traceparent": traceparent}

        status = "Approved" if result.approved else "Blocked"
        result_metadata = result.metadata or {}
        audit_document = {
            "status": status,
            "reason": result.reason,
            "subject_source": payload.get("_subject", "cio.intent"),
            "subject_target": self.target_subject,
            "potential_pnl": self._extract_potential_pnl(payload),
            "pnl_metadata": {
                "potential_pnl": self._extract_potential_pnl(payload),
                "saved_capital": float(result_metadata.get("saved_capital", 0.0)),
            },
            "veto_type": result_metadata.get("veto_type"),
            "regime_metadata": {
                "current_regime": result_metadata.get("current_regime"),
                "vol_threshold_breach": result_metadata.get("vol_threshold_breach"),
                "drawdown_limit_exceeded": result_metadata.get(
                    "drawdown_limit_exceeded"
                ),
                "scale_factor": result_metadata.get("scale_factor"),
            },
            "payload": payload,
            "traceparent": traceparent,
        }

        if result.approved:
            await self.nats_client.publish(
                self.target_subject,
                json.dumps(promoted_payload, separators=(",", ":")).encode(),
                headers=headers,
            )

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        audit_document["processing_ms"] = elapsed_ms
        audit_document["latency_budget_ms"] = self.max_latency_ms
        audit_document["latency_budget_met"] = elapsed_ms < self.max_latency_ms

        await self.roi_logger.log_audit(audit_document)

        return {
            "approved": result.approved,
            "status": status,
            "processing_ms": elapsed_ms,
            "latency_budget_met": elapsed_ms < self.max_latency_ms,
        }

    @staticmethod
    def _decode_payload(data: bytes | str) -> dict[str, Any]:
        if isinstance(data, bytes):
            return json.loads(data.decode())
        if isinstance(data, str):
            return json.loads(data)
        raise TypeError("Intent payload must be bytes or JSON string")

    @staticmethod
    def _normalize_headers(headers: Any) -> dict[str, str]:
        if not headers:
            return {}
        return {str(k).lower(): str(v) for k, v in dict(headers).items()}

    @staticmethod
    def _extract_traceparent(headers: dict[str, str] | None) -> str | None:
        if not headers:
            return None
        return headers.get("traceparent")

    @staticmethod
    def _extract_potential_pnl(payload: dict[str, Any]) -> float:
        for key in ("potential_pnl", "estimated_pnl", "expected_pnl"):
            if key in payload:
                try:
                    return float(payload[key])
                except (TypeError, ValueError):
                    break
        return 0.0
