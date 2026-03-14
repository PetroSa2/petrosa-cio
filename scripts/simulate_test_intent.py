import asyncio
import json
import logging
import os
import sys
import uuid

from nats.aio.client import Client as NATS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("simulate-intent")


async def run():
    # 1. Configuration
    nats_url = os.getenv("NATS_URL", "nats://nats-server.nats.svc.cluster.local:4222")
    topic = os.getenv("NATS_TOPIC_INTENTS", "cio.intent.trading")
    correlation_id = uuid.uuid4().hex

    # 2. Payload for inside_bar_breakout
    payload = {
        "symbol": "BTCUSDT",
        "strategy_id": "inside_bar_breakout",
        "side": "long",
        "current_price": 50000.0,
        "signal_summary": "Priority 0 Integration Test: Inside bar breakout signal detected.",
        "volatility_percentile": 0.85,
        "trend_strength": 0.32,
        "price_action_character": "Volatile",
        "trigger_type": "SIGNAL",
    }

    # 3. Connect and Publish
    nc = NATS()
    try:
        # We need to reach the cluster NATS from local, so we might need a port-forward
        # But if we run this via kubectl exec, it's easier.
        # Let's assume NATS_URL is correctly set for cluster internal reach.
        await nc.connect(nats_url)
        logger.info(f"Connected to NATS at {nats_url}")

        # Send with correlation_id in header
        headers = {"correlation_id": correlation_id}

        logger.info(
            f"Publishing test intent for inside_bar_breakout to {topic} [ID: {correlation_id}]"
        )
        await nc.publish(topic, json.dumps(payload).encode(), headers=headers)

        logger.info("Test intent published successfully.")
        await nc.flush()
    except Exception as e:
        logger.error(f"Failed to publish intent: {e}")
    finally:
        await nc.close()


if __name__ == "__main__":
    asyncio.run(run())
