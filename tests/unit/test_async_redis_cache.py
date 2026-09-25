from unittest.mock import AsyncMock

import pytest

from cio.core.cache import AsyncRedisCache


@pytest.mark.asyncio
async def test_hash_values_decode_and_nx_is_forwarded():
    redis = AsyncMock()
    redis.hget.return_value = b"value"
    redis.hgetall.return_value = {b"field": b"value"}
    redis.set.return_value = "OK"
    cache = AsyncRedisCache(redis)

    assert await cache.hget("key", "field") == "value"
    assert await cache.hgetall("key") == {"field": "value"}
    assert await cache.set_if_absent("key", "value", 120) is True
    redis.set.assert_awaited_once_with("key", "value", ex=120, nx=True)


@pytest.mark.asyncio
async def test_hash_operations_fail_closed():
    redis = AsyncMock()
    redis.hget.side_effect = RuntimeError("down")
    redis.hgetall.side_effect = RuntimeError("down")
    redis.hset.side_effect = RuntimeError("down")
    redis.hdel.side_effect = RuntimeError("down")
    redis.set.side_effect = RuntimeError("down")
    cache = AsyncRedisCache(redis)

    assert await cache.hget("key", "field") is None
    assert await cache.hgetall("key") == {}
    assert await cache.set_if_absent("key", "value", 120) is False
    await cache.hset("key", "field", "value")
    await cache.hdel("key", "field")
