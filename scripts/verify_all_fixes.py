import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from cio.clients.llm_client import DEFAULT_FALLBACK_MODEL, DEFAULT_PRIMARY_MODEL
from cio.core.context_builder import ContextBuilder
from cio.core.engine import CodeEngine
from cio.core.router import OutputRouter
from cio.models import (
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    RegimeEnum,
    RegimeFit,
    RiskLimits,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
    VolatilityLevel,
)

# Add petrosa-cio to path
sys.path.append(os.getcwd())


async def verify_fixes():
    print("🚀 Starting local verification of all 5 fixes...")
    logging.basicConfig(level=logging.ERROR)

    # --- FIX 2 VERIFICATION (Quarantine) ---
    print("\n[Fix 2] Verifying MCP Quarantine Stubs...")
    try:
        from cio.stubs.roi_engine import ShadowROIEngine

        assert ShadowROIEngine is not None
        print("✅ Fix 2 Verified: ShadowROIEngine stub exists.")
    except ImportError as e:
        print(f"❌ Fix 2 Failed: ShadowROIEngine stub import failed: {e}")
        sys.exit(1)

    # --- FIX 5 VERIFICATION (Constants) ---
    print("\n[Fix 5] Verifying model constants...")
    assert DEFAULT_PRIMARY_MODEL == "anthropic/claude-3-haiku-20240307"
    assert DEFAULT_FALLBACK_MODEL == "openai/gpt-4o-mini"
    print("✅ Fix 5 Verified: Constants are correctly pinned.")

    # --- FIX 3 VERIFICATION ---
    print("\n[Fix 3] Verifying SECURITY_WARNING logging when token is missing...")
    with patch.dict(os.environ, {"PETROSA_INTERNAL_TOKEN": ""}):
        with patch("cio.core.router.logger.warning") as mock_warn_router:
            router = OutputRouter(
                AsyncMock(), AsyncMock(), "http://ta-bot", "http://realtime"
            )
            warn_router_called = any(
                "SECURITY_WARNING" in str(call)
                for call in mock_warn_router.call_args_list
            )
            assert warn_router_called

        with patch("cio.core.context_builder.logger.warning") as mock_warn_builder:
            ContextBuilder("http://data-manager", "http://tradeengine")
            warn_builder_called = any(
                "SECURITY_WARNING" in str(call)
                for call in mock_warn_builder.call_args_list
            )
            assert warn_builder_called
    print("✅ Fix 3 Verified: SECURITY_WARNING fires when token is missing.")

    # --- FIX 4 VERIFICATION (Regime Awareness) ---
    print("\n[Fix 4] Verifying Regime Hard Blocks...")
    context_capitulation = MagicMock(spec=TriggerContext)
    context_capitulation.regime = MagicMock()
    context_capitulation.regime.regime = RegimeEnum.CAPITULATION
    context_capitulation.correlation_id = "test-id"
    context_capitulation.global_drawdown_pct = 0.0
    context_capitulation.risk_limits = RiskLimits(
        max_drawdown_pct=0.1,
        max_orders_global=10,
        max_orders_per_symbol=5,
        max_position_size_usd=1000,
    )
    context_capitulation.open_orders_global = 0
    context_capitulation.open_orders_symbol = 0

    res_cap = CodeEngine.run(context_capitulation)
    assert res_cap.hard_blocked
    assert "CAPITULATION" in res_cap.block_reason
    print("✅ Fix 4 Verified: CAPITULATION triggers hard block.")

    print("[Fix 4] Verifying TP Multipliers...")
    context_bull = MagicMock(spec=TriggerContext)
    context_bull.regime = MagicMock()
    context_bull.regime.regime = RegimeEnum.TRENDING_BULL
    context_bull.strategy_defaults = StrategyDefaults(
        take_profit_pct=0.02, stop_loss_pct=0.01, leverage=3.0, max_hold_hours=24
    )
    context_bull.volatility_level = VolatilityLevel.LOW
    context_bull.strategy_stats = StrategyStats(win_rate=0.5)
    context_bull.global_drawdown_pct = 0.0
    context_bull.risk_limits = RiskLimits(
        max_drawdown_pct=0.1,
        max_orders_global=10,
        max_orders_per_symbol=5,
        max_position_size_usd=1000,
    )
    context_bull.open_orders_global = 0
    context_bull.open_orders_symbol = 0
    context_bull.available_capital_usd = 10000.0

    res_bull = CodeEngine.run(context_bull)
    assert abs(res_bull.recommended_tp_pct - 0.026) < 0.0001
    assert res_bull.leverage == 2.0
    print("✅ Fix 4 Verified: TRENDING_BULL applies TP multiplier and leverage cap.")

    # --- FIX 1 VERIFICATION (REST over NATS) ---
    print("\n[Fix 1] Verifying PAUSE_STRATEGY uses REST POST instead of NATS...")
    mock_nats = AsyncMock()
    mock_cache = AsyncMock()
    router = OutputRouter(
        mock_nats, AsyncMock(), "http://ta-bot", "http://realtime", cache=mock_cache
    )

    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "momentum_pulse"
    context.correlation_id = "rest-test-id"

    decision_pause = DecisionResult(
        hard_blocked=False,
        ev_passes=False,
        cost_viable=False,
        regime_confidence=ConfidenceLevel.LOW,
        regime_fit=RegimeFit.NEUTRAL,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=ActionType.PAUSE_STRATEGY,
        justification="Test pause",
        thought_trace="Testing Fix 1",
    )

    with patch.object(router.http_client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.status_code = 200
        await router.route(context, decision_pause)
        assert mock_nats.publish.call_count == 0
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "http://ta-bot/api/v1/strategies/momentum_pulse/config" in call_url

    print("✅ Fix 1 Verified: PAUSE_STRATEGY uses REST POST to TA-bot.")

    print("\n🎉 ALL FIXES VERIFIED LOCALLY!")


if __name__ == "__main__":
    asyncio.run(verify_fixes())
