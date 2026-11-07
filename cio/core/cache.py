import logging

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class CacheUnavailable(Exception):
    """Redis could not be read, even after the client's bounded retry (petrosa-cio#406).

    Raised only by the strict reads. A caller that gates trading on cached state must not treat it as a miss.
    """


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

    async def get_strict(self, key: str) -> str | None:
        """Like ``get``, but a Redis error raises ``CacheUnavailable`` instead of looking like an absent key.

        petrosa-cio#406: ``get`` returns None on an error, which an entry gate would read as "no prior signal"
        (fail open). Gates use this read and decide themselves what an unreachable Redis means.
        """
        try:
            value = await self.redis.get(key)
        except Exception as e:
            raise CacheUnavailable(f"{type(e).__name__}: {e}") from e
        if value:
            return value.decode("utf-8") if isinstance(value, bytes) else value
        return None

    async def set(self, key: str, value: str, ttl: int = 900):
        """Stores a value in the cache with a TTL (default 15 mins)."""
        try:
            await self.redis.set(key, value, ex=ttl)
        except Exception as e:
            logger.error(f"Redis set error for key {key}: {e}")

    async def delete(self, key: str) -> None:
        """Removes a key from the cache (#199 — context-gate auto-unfreeze
        needs to clear a stale freeze/streak marker immediately rather
        than waiting out its TTL)."""
        try:
            await self.redis.delete(key)
        except Exception as e:
            logger.error(f"Redis delete error for key {key}: {e}")

    async def hget(self, key: str, field: str) -> str | None:
        try:
            value = await self.redis.hget(key, field)
            if value is None:
                return None
            return value.decode("utf-8") if isinstance(value, bytes) else value
        except Exception as e:
            logger.error(f"Redis hget error for key {key}: {e}")
            return None

    async def hset(self, key: str, field: str, value: str) -> None:
        try:
            await self.redis.hset(key, field, value)
        except Exception as e:
            logger.error(f"Redis hset error for key {key}: {e}")

    async def hdel(self, key: str, field: str) -> None:
        try:
            await self.redis.hdel(key, field)
        except Exception as e:
            logger.error(f"Redis hdel error for key {key}: {e}")

    async def hgetall(self, key: str) -> dict[str, str]:
        try:
            values = await self.redis.hgetall(key)
            return {
                field.decode("utf-8")
                if isinstance(field, bytes)
                else field: value.decode("utf-8") if isinstance(value, bytes) else value
                for field, value in values.items()
            }
        except Exception as e:
            logger.error(f"Redis hgetall error for key {key}: {e}")
            return {}

    async def set_if_absent(self, key: str, value: str, ttl: int) -> bool:
        try:
            result = await self.redis.set(key, value, ex=ttl, nx=True)
            return bool(result)
        except Exception as e:
            logger.error(f"Redis set_if_absent error for key {key}: {e}")
            return False
