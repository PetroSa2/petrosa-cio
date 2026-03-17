import asyncio
import json
from cio.core.rate_governor import RateGovernor, RateLimitStatus

async def test_throttling():
    governor = RateGovernor(nats_url="nats://localhost:4222")
    
    # 1. Test initial state
    print(f"Initial throttled: {governor.is_throttled()}")
    assert governor.is_throttled() is False
    
    # 2. Test high weight
    governor.current_weight = 1100
    print(f"Status at 1100: {governor.get_status()}")
    assert governor.is_throttled() is True
    
    # 3. Test normal weight
    governor.current_weight = 500
    print(f"Status at 500: {governor.get_status()}")
    assert governor.is_throttled() is False
    
    print("Throttling logic test passed!")

if __name__ == "__main__":
    asyncio.run(test_throttling())
