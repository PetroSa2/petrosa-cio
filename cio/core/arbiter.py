import logging

from cio.core.cache import AsyncRedisCache

logger = logging.getLogger(__name__)

_DEDUP_TTL_SECONDS = 60
_CONFLICT_TTL_SECONDS = 300  # 5 minutes


class SignalArbiter:
    """
    Cross-strategy signal arbitration layer.

    Responsibilities:
    - Deduplication: drop redundant signals (same symbol + action) within 60s.
    - Conflict resolution: when two strategies issue opposing signals for the
      same symbol within 5 minutes, only the higher-confidence signal is allowed.

    State is stored in Redis so arbitration is consistent across multiple CIO
    replicas sharing the same Redis instance.
    """

    def __init__(self, cache: AsyncRedisCache) -> None:
        self._cache = cache

    async def check(
        self,
        symbol: str,
        action: str,
        confidence: float,
        strategy_id: str,
        correlation_id: str,
    ) -> tuple[bool, str]:
        """
        Check whether the incoming signal should be allowed through.

        Returns:
            (allowed, reason) — ``allowed=False`` means the signal must be suppressed.
        """
        action_lower = action.lower()

        # 1. Deduplication guard (60 s window, same symbol + action)
        dedup_key = f"arbiter:dedup:{symbol}:{action_lower}"
        if await self._cache.get(dedup_key):
            reason = (
                f"SIGNAL_DEDUPLICATED: {symbol} {action_lower} already published "
                f"within the last {_DEDUP_TTL_SECONDS}s (strategy={strategy_id})"
            )
            logger.info(reason, extra={"correlation_id": correlation_id})
            return False, reason

        # 2. Conflict detection (5 min window, opposing action for same symbol)
        bias_key = f"arbiter:bias:{symbol}"
        raw_bias = await self._cache.get(bias_key)
        if raw_bias:
            try:
                stored_action, stored_conf_str, stored_strategy = raw_bias.split(":", 2)
                stored_conf = float(stored_conf_str)
            except ValueError:
                stored_action = stored_conf_str = stored_strategy = None
                stored_conf = 0.0

            if stored_action and stored_action != action_lower:
                # Opposing signal detected within the conflict window
                if confidence <= stored_conf:
                    reason = (
                        f"signal_conflict_resolved: {symbol} {action_lower} from "
                        f"{strategy_id} (confidence={confidence:.3f}) suppressed in favour "
                        f"of {stored_action} from {stored_strategy} "
                        f"(confidence={stored_conf:.3f})"
                    )
                    logger.info(reason, extra={"correlation_id": correlation_id})
                    return False, reason
                else:
                    # Incoming signal wins — overwrite stored bias
                    logger.info(
                        f"signal_conflict_resolved: {symbol} {action_lower} from "
                        f"{strategy_id} (confidence={confidence:.3f}) wins over "
                        f"{stored_action} from {stored_strategy} "
                        f"(confidence={stored_conf:.3f})",
                        extra={"correlation_id": correlation_id},
                    )

        # 3. Signal is allowed — record state in Redis
        bias_value = f"{action_lower}:{confidence:.6f}:{strategy_id}"
        await self._cache.set(bias_key, bias_value, ttl=_CONFLICT_TTL_SECONDS)
        await self._cache.set(dedup_key, "1", ttl=_DEDUP_TTL_SECONDS)

        return True, "allowed"
