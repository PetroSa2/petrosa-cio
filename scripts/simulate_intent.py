import asyncio
import json
import os
import uuid
import logging
import sys
from nats.aio.client import Client as NATS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("simulate-intent")

async def run():
    # 1. Configuration
    nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
    topic = os.getenv("NATS_TOPIC_INTENTS", "cio.intent.trading")
    correlation_id = uuid.uuid4().hex

    # 2. Payload
    payload = {
        "symbol": "BTCUSDT",
        "strategy_id": "momentum_v1",
        "side": "long",
        "current_price": 50000.0,
        "signal_summary": "Strong breakout detected on 15m timeframe with high volume confirmation.",
        "volatility_percentile": 0.65,
        "trend_strength": 0.82,
        "price_action_character": "Impulsive"
    }

    # 3. Connect and Publish
    nc = NATS()
    try:
        await nc.connect(nats_url)
        logger.info(f"Connected to NATS at {nats_url}")

        # Send with correlation_id in header
        headers = {"correlation_id": correlation_id}
        
        logger.info(f"Publishing intent to {topic} [ID: {correlation_id}]")
        await nc.publish(
            topic, 
            json.dumps(payload).encode(),
            headers=headers
        )
        
        logger.info("Intent published successfully.")
        await nc.flush()
    except Exception as e:
        logger.error(f"Failed to publish intent: {e}")
    finally:
        await nc.close()

if __name__ == "__main__":
    asyncio.run(run())
