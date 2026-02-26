"""Tests for governance earnings API handler."""

import pytest

import main


class FakeRoiEngine:
    async def get_earnings_summary(self, window_hours: int = 168):
        return {
            "actual_pnl": 20.0,
            "shadow_roi": 8.0,
            "window_hours": window_hours,
        }


@pytest.mark.asyncio
async def test_get_earnings_summary_endpoint_uses_roi_engine():
    main.app.state.roi_engine = FakeRoiEngine()
    result = await main.get_earnings_summary(window_hours=12)
    assert result["actual_pnl"] == pytest.approx(20.0)
    assert result["shadow_roi"] == pytest.approx(8.0)
    assert result["window_hours"] == 12
