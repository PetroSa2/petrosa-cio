"""Institutional memory service for vector indexing and retrieval."""

from __future__ import annotations

from typing import Any

from core.db.vector_client import VectorMemoryClient


class InstitutionalMemoryService:
    """Indexes thought traces and provides semantic retrieval."""

    def __init__(self, vector_client: VectorMemoryClient | None = None):
        self.vector_client = vector_client or VectorMemoryClient()

    async def index_audit_event(self, audit_document: dict[str, Any]) -> dict[str, Any]:
        thought_trace = str(audit_document.get("thought_trace", "")).strip()
        if len(thought_trace) < 20:
            return {
                "indexed": False,
                "deduplicated": False,
                "reason": "missing_or_short_thought_trace",
            }

        metadata = {
            "audit_id": audit_document.get("audit_id"),
            "model": audit_document.get("model"),
            "event_type": audit_document.get("event_type"),
            "timestamp": audit_document.get("updated_at"),
            "pnl_impact": self._extract_pnl_impact(audit_document),
            "outcome": "success"
            if audit_document.get("event_type") == "config_update"
            else "failure",
        }
        return await self.vector_client.upsert_trace(
            trace=thought_trace, metadata=metadata
        )

    async def search_knowledge_base(self, query: str, top_k: int = 5) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query must not be empty")
        if top_k <= 0:
            raise ValueError("top_k must be a positive integer")

        results = await self.vector_client.search(query=query, top_k=top_k)
        return {
            "query": query,
            "top_k": top_k,
            "results_count": len(results),
            "results": results,
        }

    @staticmethod
    def _extract_pnl_impact(audit_document: dict[str, Any]) -> float:
        payload = audit_document.get("payload") or {}
        for key in ("potential_pnl", "expected_pnl", "estimated_pnl"):
            if key in payload:
                try:
                    return float(payload[key])
                except (TypeError, ValueError):
                    continue
        return 0.0
