import pytest
from unittest.mock import AsyncMock, MagicMock
from cio.core.roi_engine import ShadowROIEngine

@pytest.mark.asyncio
async def test_shadow_roi_calculation():
    # Mock Data Manager Client
    mock_dm = AsyncMock()
    # Mock depth response: bids/asks [[price, quantity]]
    mock_dm.get_depth.return_value = {
        "bids": [["50000.0", "1.0"]],
        "asks": [["50010.0", "1.0"]]
    }
    
    engine = ShadowROIEngine(data_manager_client=mock_dm)
    
    # 1. Test BUY blocked trade
    blocked_buy = {
        "symbol": "BTCUSDT",
        "price": 49000.0,
        "side": "BUY",
        "amount": 0.1
    }
    
    # current_price = (50000 + 50010) / 2 = 50005.0
    # expected_pnl = (50005.0 - 49000.0) * 0.1 = 100.5
    pnl = await engine.calculate_shadow_pnl(blocked_buy)
    assert pnl == pytest.approx(100.5)
    
    # 2. Test SELL blocked trade
    blocked_sell = {
        "symbol": "BTCUSDT",
        "price": 51000.0,
        "side": "SELL",
        "amount": 0.1
    }
    
    # expected_pnl = (51000.0 - 50005.0) * 0.1 = 99.5
    pnl = await engine.calculate_shadow_pnl(blocked_sell)
    assert pnl == pytest.approx(99.5)

@pytest.mark.asyncio
async def test_roi_engine_active_status():
    engine = ShadowROIEngine()
    summary = await engine.get_earnings_summary()
    
    assert summary["status"] == "ACTIVE"
    assert "shadow_roi" in summary
    assert "actual_pnl" in summary
