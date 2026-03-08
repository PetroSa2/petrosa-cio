import logging

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class AsyncRedisCache:
    """
    Async wrapper for Redis caching.
    Handles storage and retrieval of JSON-serialized domain models.
    """

    def __init__(self, redis_client: Redis):
        self.redis = redis_client

    async def get(self, key: str) -> str | None:
        """Retrieves a value from the cache."""
        try:
            value = await self.redis.get(key)
            if value:
                return value.decode("utf-8")
            return None
        except Exception as e:
            logger.error(f"Redis get error for key {key}: {e}")
            return None

    async def set(self, key: str, value: str, ttl: int = 900):
        """Stores a value in the cache with a TTL (default 15 mins)."""
        try:
            await self.redis.set(key, value, ex=ttl)
        except Exception as e:
            logger.error(f"Redis set error for key {key}: {e}")
