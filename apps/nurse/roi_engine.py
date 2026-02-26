"""Shadow ROI aggregation, fatigue checks, and Friday report generation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class ShadowROIEngine:
    """Computes governance earnings summaries from nurse audit logs."""

    def __init__(
        self,
        *,
        audit_collection: Any | None = None,
        summary_collection: Any | None = None,
        report_dir: str = "docs/reports",
        aggregation_interval_seconds: int = 15 * 60,
    ):
        self.audit_collection = audit_collection
        self.summary_collection = summary_collection
        self.report_dir = Path(report_dir)
        self.aggregation_interval_seconds = aggregation_interval_seconds
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        self._last_report_key: str | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            now = datetime.now(UTC)
            await self.aggregate_window(
                start=now - timedelta(minutes=15),
                end=now,
                persist=True,
            )
            await self.generate_friday_report(now=now)
            await asyncio.sleep(self.aggregation_interval_seconds)

    async def get_earnings_summary(self, window_hours: int = 24 * 7) -> dict[str, Any]:
        now = datetime.now(UTC)
        start = now - timedelta(hours=window_hours)
        summary = await self.aggregate_window(start=start, end=now, persist=False)
        summary["fatigue"] = self.check_strategy_fatigue(
            actual_pnl=summary["actual_pnl"],
            shadow_roi=summary["shadow_roi"],
        )
        return summary

    async def aggregate_window(
        self,
        *,
        start: datetime,
        end: datetime,
        persist: bool,
    ) -> dict[str, Any]:
        docs = await self._load_audits(start=start, end=end)
        actual_pnl = 0.0
        shadow_roi = 0.0
        saved_capital = 0.0
        approved_signals = 0
        blocked_intents = 0

        for doc in docs:
            status = str(doc.get("status", "")).lower()
            if status == "approved":
                approved_signals += 1
                actual_pnl += self._to_float(
                    doc.get("actual_pnl", doc.get("realized_pnl", 0.0))
                )
            elif status == "blocked":
                blocked_intents += 1
                potential = self._to_float(
                    doc.get("potential_pnl", doc.get("expected_loss", 0.0))
                )
                saved = self._to_float(
                    (doc.get("pnl_metadata") or {}).get("saved_capital")
                )
                shadow_roi += max(potential, saved)
                saved_capital += saved

        governance_status = "ACTIVE" if blocked_intents > 0 else "OBSERVE"
        summary = {
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "actual_pnl": round(actual_pnl, 6),
            "shadow_roi": round(shadow_roi, 6),
            "saved_capital": round(saved_capital, 6),
            "approved_signals": approved_signals,
            "blocked_intents": blocked_intents,
            "total_events": len(docs),
            "governance_status": governance_status,
        }

        if persist and self.summary_collection is not None:
            await self.summary_collection.insert_one(dict(summary))

        return summary

    @staticmethod
    def check_strategy_fatigue(
        *,
        actual_pnl: float,
        shadow_roi: float,
        threshold_ratio: float = 1.0,
    ) -> dict[str, Any]:
        baseline = abs(actual_pnl) if actual_pnl != 0 else 1.0
        ratio = shadow_roi / baseline
        flagged = shadow_roi > max(actual_pnl, 0.0) and ratio > threshold_ratio
        return {
            "flagged": flagged,
            "reason": (
                "Saved capital is consistently above realized PnL"
                if flagged
                else "Safety profile is within expected bounds"
            ),
            "shadow_to_actual_ratio": round(ratio, 6),
        }

    async def generate_friday_report(self, now: datetime | None = None) -> str | None:
        now_utc = now or datetime.now(UTC)
        if now_utc.weekday() != 4 or now_utc.hour != 16:
            return None

        week_key = now_utc.strftime("%Y-%W")
        if self._last_report_key == week_key:
            return None

        summary = await self.get_earnings_summary(window_hours=24 * 7)
        report = self._render_report(summary=summary, generated_at=now_utc)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        report_path = (
            self.report_dir / f"earnings-report-{now_utc.date().isoformat()}.md"
        )
        report_path.write_text(report, encoding="utf-8")
        self._last_report_key = week_key
        return str(report_path)

    def _render_report(self, *, summary: dict[str, Any], generated_at: datetime) -> str:
        fatigue = summary["fatigue"]
        return (
            f"# Friday Earnings Report ({generated_at.date().isoformat()})\n\n"
            f"- Actual Profit: ${summary['actual_pnl']:.2f}\n"
            f"- Saved Capital (Shadow ROI): ${summary['shadow_roi']:.2f}\n"
            f"- Governance: {summary['governance_status']}\n"
            f"- Approved Signals: {summary['approved_signals']}\n"
            f"- Blocked Intents: {summary['blocked_intents']}\n"
            f"- Strategy Fatigue: {'YES' if fatigue['flagged'] else 'NO'}\n"
            f"- Fatigue Ratio: {fatigue['shadow_to_actual_ratio']:.4f}\n"
            f"- Fatigue Reason: {fatigue['reason']}\n"
        )

    async def _load_audits(
        self, *, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        if self.audit_collection is None:
            return []

        if hasattr(self.audit_collection, "find_window"):
            docs = await self.audit_collection.find_window(start=start, end=end)
            return list(docs)

        if hasattr(self.audit_collection, "find"):
            cursor = self.audit_collection.find(
                {"logged_at": {"$gte": start.isoformat(), "$lt": end.isoformat()}}
            )
            if hasattr(cursor, "to_list"):
                return await cursor.to_list(length=10_000)
            return [item async for item in cursor]

        return []

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
