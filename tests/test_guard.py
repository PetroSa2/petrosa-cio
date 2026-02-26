"""Tests for market regime semantic guard."""

import json

import pytest

from apps.nurse.enforcer import NurseEnforcer
from apps.nurse.guard import RegimeGuard


class FakeRedis:
    def __init__(self, payload: dict):
        self.payload = payload

    async def get(self, key: str):
        _ = key
        return json.dumps(self.payload)


@pytest.mark.asyncio
async def test_regime_guard_blocks_trade_on_drawdown_limit():
    guard = RegimeGuard(
        redis_client=FakeRedis(
            {
                "current_regime": "bearish",
                "vol_threshold_breach": False,
                "drawdown_limit_exceeded": True,
                "current_drawdown": 0.05,
            }
        )
    )

    decision = await guard.evaluate({"action": "buy", "quantity": 2.0})

    assert decision.approved is False
    assert decision.veto_reason == "drawdown_limit_exceeded"
    assert decision.metadata["current_regime"] == "bearish"
    assert decision.saved_capital == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_enforcer_scales_position_on_volatility_breach():
    guard = RegimeGuard(
        redis_client=FakeRedis(
            {
                "current_regime": "high_vol",
                "vol_threshold_breach": True,
                "drawdown_limit_exceeded": False,
                "current_drawdown": 0.02,
                "scale_down_factor": 0.4,
            }
        )
    )
    enforcer = NurseEnforcer(regime_guard=guard)

    result = await enforcer.enforce({"action": "buy", "quantity": 5.0})

    assert result.approved is True
    assert result.metadata is not None
    assert result.metadata["current_regime"] == "high_vol"
    assert result.metadata["scale_factor"] == pytest.approx(0.4)
    assert result.metadata["scaled_quantity"] == pytest.approx(2.0)
    assert result.metadata["saved_capital"] == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_enforcer_returns_semantic_veto_metadata_when_blocked():
    guard = RegimeGuard(
        redis_client=FakeRedis(
            {
                "current_regime": "drawdown",
                "vol_threshold_breach": False,
                "drawdown_limit_exceeded": True,
                "current_drawdown": 0.03,
            }
        )
    )
    enforcer = NurseEnforcer(regime_guard=guard)

    result = await enforcer.enforce({"action": "sell", "quantity": 4.0})

    assert result.approved is False
    assert result.reason == "drawdown_limit_exceeded"
    assert result.metadata is not None
    assert result.metadata["veto_type"] == "semantic"
    assert result.metadata["saved_capital"] == pytest.approx(0.12)
