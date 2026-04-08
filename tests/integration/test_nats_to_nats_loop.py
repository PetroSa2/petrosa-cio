import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cio.apps.nurse.enforcer import NurseEnforcer
from cio.core.context_builder import ContextBuilder
from cio.core.listener import NATSListener
from cio.core.orchestrator import Orchestrator
from cio.core.router import OutputRouter


@pytest.mark.asyncio
async def test_full_nats_to_nats_loop():
    """
    Integration test for the full CIO reasoning loop with T-Junction logic.
    NATS Intent -> HTTP Gathers -> Code Engine -> Mock LLM -> NATS Legacy + Modern.
    """

    # 1. Setup Mocks
    mock_nc = AsyncMock()

    # Define the mock data payloads
    regime_data = {
        "pair": "BTCUSDT",
        "metric": "regime",
        "data": {
            "regime": "bullish_acceleration",
            "volatility_level": "medium",
            "volume_level": "high",
            "trend_direction": "up",
            "confidence": "0.95",
        },
        "metadata": {"timestamp": "2026-03-08T17:00:00Z", "collection": "live"},
    }

    tradeengine_data = {
        "portfolio": {
            "gross_exposure": 0.1,
            "same_asset_pct": 0.05,
            "open_positions_count": 1,
        },
        "risk_limits": {
            "max_drawdown_pct": 0.15,
            "max_orders_global": 50,
            "max_orders_per_symbol": 5,
            "max_position_size_usd": 5000.0,
        },
        "env_stats": {
            "global_drawdown_pct": 0.02,
            "open_orders_global": 5,
            "open_orders_symbol": 0,
            "available_capital_usd": 50000.0,
        },
    }

    strategy_data = {
        "stats": {
            "win_rate": 0.65,
            "win_rate_delta": 0.05,
            "consecutive_losses": 0,
            "recent_pnl_trend": "positive",
        },
        "defaults": {
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.06,
            "leverage": 1.0,
            "max_hold_hours": 12.0,
        },
    }

    # Helper to create a mock response
    def create_mock_response(json_data):
        m = MagicMock(spec=httpx.Response)
        m.status_code = 200
        m.json.return_value = json_data
        m.raise_for_status.return_value = None
        return m

    # Mock the AsyncClient.get method
    async def mock_get(url, **kwargs):
        url_str = str(url)
        if "analysis/regime" in url_str:
            return create_mock_response(regime_data)
        if "tradeengine/state" in url_str:
            return create_mock_response(tradeengine_data)
        if "analysis/performance" in url_str:
            return create_mock_response(strategy_data)
        if "config/strategies" in url_str:
            # Wrap parameters for strategy DNA endpoint
            return create_mock_response({"parameters": strategy_data["defaults"]})
        return create_mock_response({})

    # 2. Instantiate the full stack with mocked environment
    mock_cache = MagicMock()
    mock_cache.get = AsyncMock(return_value=None)
    mock_cache.set = AsyncMock()

    with patch.dict("os.environ", {"LLM_PROVIDER": "mock", "DRY_RUN": "false"}):
        with patch("httpx.AsyncClient.get", side_effect=mock_get):
            builder = ContextBuilder(
                data_manager_url="http://data-manager",
                tradeengine_url="http://tradeengine",
            )

            orchestrator = Orchestrator(cache=mock_cache)
            original_run = orchestrator.run
            orchestrator.run = AsyncMock(side_effect=original_run)
            enforcer = NurseEnforcer(orchestrator=orchestrator)
            mock_vc = AsyncMock()
            router = OutputRouter(
                nats_client=mock_nc,
                vector_client=mock_vc,
                ta_bot_url="http://ta-bot",
                realtime_strategies_url="http://realtime",
            )

            listener = NATSListener(
                nats_client=mock_nc,
                enforcer=enforcer,
                context_builder=builder,
                router=router,
            )

            # 3. Create a dummy NATS message
            mock_msg = MagicMock()
            mock_msg.subject = "cio.intent.trading.momentum_v1"
            mock_msg.data = json.dumps(
                {
                    "symbol": "BTCUSDT",
                    "strategy_id": "momentum_v1",
                    "side": "long",
                    "current_price": 50000.0,
                    "signal_summary": "Strong breakout",
                    "volatility_percentile": 0.4,
                    "trend_strength": 0.9,
                    "price_action_character": "Impulsive",
                }
            ).encode()
            mock_msg.headers = {"correlation_id": "test-loop-id"}

            # 4. Execute the loop via the listener's handler
            await listener._handle_message(mock_msg)

            # 5. Assertions
            # Verify NATS publish was called TWICE (Legacy + Modern)
            assert mock_nc.publish.call_count == 2
            assert orchestrator.run.await_count == 1

            # Check Legacy Call (signals.trading.<strategy_id> — matches tradeengine signals.trading.>)
            legacy_call = mock_nc.publish.call_args_list[0]
            assert legacy_call[0][0] == "signals.trading.momentum_v1"
            legacy_payload = json.loads(legacy_call[0][1].decode())
            assert legacy_payload["action"] == "buy"
            # Verify quantity fix: size / price.
            # CodeEngine size ~ $5000 (capped), price 50000 -> qty 0.1
            assert legacy_payload["quantity"] == pytest.approx(0.1)

            # Check Modern Call (trade.execute.momentum_v1)
            modern_call = mock_nc.publish.call_args_list[1]
            assert modern_call[0][0] == "trade.execute.momentum_v1"
            modern_payload = json.loads(modern_call[0][1].decode())
            assert modern_payload["action"] == "execute"
            assert modern_payload["computed_position_size_usd"] == pytest.approx(5000.0)

            # Verify Vector Upsert (Audit Path)
            mock_vc.upsert.assert_called_once()

            await builder.close()
