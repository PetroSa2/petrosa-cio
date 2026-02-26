"""Tests for Shadow ROI engine aggregation and reporting."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.nurse.roi_engine import ShadowROIEngine


class FakeAuditCollection:
    def __init__(self, documents):
        self.documents = documents

    async def find_window(self, *, start, end):
        _ = (start, end)
        return list(self.documents)


class FakeSummaryCollection:
    def __init__(self):
        self.documents = []

    async def insert_one(self, document):
        self.documents.append(document)


@pytest.mark.asyncio
async def test_aggregate_window_computes_actual_and_shadow_roi():
    audits = FakeAuditCollection(
        [
            {"status": "Approved", "actual_pnl": 12.5},
            {
                "status": "Blocked",
                "potential_pnl": 4.0,
                "pnl_metadata": {"saved_capital": 3.0},
            },
            {
                "status": "Blocked",
                "potential_pnl": 1.0,
                "pnl_metadata": {"saved_capital": 6.0},
            },
        ]
    )
    summaries = FakeSummaryCollection()
    engine = ShadowROIEngine(audit_collection=audits, summary_collection=summaries)

    summary = await engine.aggregate_window(
        start=datetime(2026, 2, 20, 10, 0, tzinfo=UTC),
        end=datetime(2026, 2, 20, 10, 15, tzinfo=UTC),
        persist=True,
    )

    assert summary["actual_pnl"] == pytest.approx(12.5)
    assert summary["shadow_roi"] == pytest.approx(10.0)
    assert summary["saved_capital"] == pytest.approx(9.0)
    assert summary["approved_signals"] == 1
    assert summary["blocked_intents"] == 2
    assert len(summaries.documents) == 1


def test_check_strategy_fatigue_flags_safety_tax_condition():
    fatigue = ShadowROIEngine.check_strategy_fatigue(actual_pnl=5.0, shadow_roi=12.0)
    assert fatigue["flagged"] is True
    assert fatigue["shadow_to_actual_ratio"] == pytest.approx(2.4)


@pytest.mark.asyncio
async def test_generate_friday_report_writes_markdown_file(tmp_path: Path):
    audits = FakeAuditCollection(
        [
            {"status": "Approved", "actual_pnl": 10.0},
            {"status": "Blocked", "potential_pnl": 4.0},
        ]
    )
    engine = ShadowROIEngine(audit_collection=audits, report_dir=str(tmp_path))

    report_path = await engine.generate_friday_report(
        now=datetime(2026, 2, 27, 16, 0, tzinfo=UTC)
    )

    assert report_path is not None
    report_text = Path(report_path).read_text(encoding="utf-8")
    assert "Friday Earnings Report" in report_text
    assert "Actual Profit: $10.00" in report_text
    assert "Saved Capital (Shadow ROI): $4.00" in report_text
