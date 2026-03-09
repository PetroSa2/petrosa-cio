import asyncio
import json
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from cio.core.context_builder import ContextBuilder
from cio.core.listener import NATSListener
from cio.core.orchestrator import Orchestrator
from cio.core.router import OutputRouter

# Configure logging to stdout for audit
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("live-heartbeat")


async def run_simulation():
    logger.info("--- Starting CIO Heartbeat Simulation (Shadow Mode) ---")

    # 1. Setup Environment
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["DRY_RUN"] = "true"

    # 2. Mock NATS and HTTP Responses (Matching simulate_intent.py scenario)
    mock_nc = AsyncMock()

    # Mock HTTP responses for context gathering
    async def mock_get(url, **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.raise_for_status.return_value = None

        if "analysis/regime" in str(url):
            m.json.return_value = {
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
        elif "tradeengine/state" in str(url):
            m.json.return_value = {
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
        elif "strategy" in str(url):
            m.json.return_value = {
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
        return m

    # 3. Instantiate Stack
    with patch("httpx.AsyncClient.get", side_effect=mock_get):
        builder = ContextBuilder(
            data_manager_url="http://data-manager",
            tradeengine_url="http://tradeengine",
        )
        from cio.core.vector import MockVectorClient

        vector_client = MockVectorClient()

        orchestrator = Orchestrator()
        router = OutputRouter(nats_client=mock_nc, vector_client=vector_client)
        listener = NATSListener(
            nats_client=mock_nc,
            orchestrator=orchestrator,
            context_builder=builder,
            router=router,
        )

        # 4. Simulated Intent (matching scripts/simulate_intent.py)
        mock_msg = MagicMock()
        mock_msg.subject = "cio.intent.trading"
        mock_msg.data = json.dumps(
            {
                "symbol": "BTCUSDT",
                "strategy_id": "momentum_v1",
                "side": "long",
                "current_price": 50000.0,
                "signal_summary": "Strong breakout detected on 15m timeframe with high volume confirmation.",
                "volatility_percentile": 0.65,
                "trend_strength": 0.82,
                "price_action_character": "Impulsive",
            }
        ).encode()
        mock_msg.headers = {"correlation_id": "sim-heartbeat-001"}

        # 5. Fire!
        await listener._handle_message(mock_msg)

        logger.info("--- Simulation Complete ---")
        await builder.close()


if __name__ == "__main__":
    asyncio.run(run_simulation())
