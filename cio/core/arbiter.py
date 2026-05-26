"""Signal arbitration layer for cross-strategy deduplication and conflict resolution."""

import logging
from typing import TYPE_CHECKING

from cio.core.cache import AsyncRedisCache

if TYPE_CHECKING:
    from cio.core.evaluator_subscriber import EvaluatorSubscriber

logger = logging.getLogger(__name__)

_DEDUP_TTL_SECONDS = 60
_CONFLICT_TTL_SECONDS = 300  # 5 minutes

# P2.6 (#597) + P2.6-EXT (#123): subsystems whose unhealthy verdict triggers
# pause behavior on every incoming signal. `ingest` was the first wired source
# (#593); execution, strategy-fidelity, and audit added by #123 (FR45 → GREEN).
_PAUSE_GUARD_SUBSYSTEMS: tuple[str, ...] = (
    "ingest",
    "execution",
    "strategy-fidelity",
    "audit",
)

# P2.6-EXT (#123): per-subsystem pause policy.
# "strict" = suppress the signal entirely (safe default for data-integrity subsystems).
# "lax"    = emit a warning but allow the signal through (suitable for advisory subsystems
#             like strategy-fidelity and audit where a transient unhealthy state should not
#             halt arbitration cold).
_PAUSE_GUARD_POLICY: dict[str, str] = {
    "ingest": "strict",
    "execution": "strict",
    "strategy-fidelity": "lax",
    "audit": "lax",
}

# Canonical action mapping: normalise producer-specific side names to buy/sell.
_SIDE_NORMALISE: dict[str, str] = {
    "buy": "buy",
    "long": "buy",
    "bullish": "buy",
    "sell": "sell",
    "short": "sell",
    "bearish": "sell",
}


def _normalise_action(raw: str) -> str:
    """Map producer-specific side names (long/short/bullish/bearish) to buy/sell."""
    return _SIDE_NORMALISE.get(raw.lower(), raw.lower())


class SignalArbiter:
    """
    Cross-strategy signal arbitration layer.

    Responsibilities:
    - Deduplication: drop redundant signals (same symbol + canonical action) within 60s.
    - Conflict resolution: when two strategies issue opposing signals for the
      same symbol within 5 minutes, only the higher-confidence signal is allowed.

    State is stored in Redis so arbitration is consistent across multiple CIO
    replicas sharing the same Redis instance.

    Note: dedup check (GET then SET) is non-atomic and may allow two signals through
    under high concurrency on multiple replicas. This is an accepted MVP trade-off;
    a Redis SETNX / Lua atomic upgrade can be added when replica counts increase.
    """

    def __init__(
        self,
        cache: AsyncRedisCache,
        evaluator_subscriber: "EvaluatorSubscriber | None" = None,
        pause_policy: "dict[str, str] | None" = None,
    ) -> None:
        self._cache = cache
        # P2.6 (#597): optional collaborator. When wired, every arbiter
        # check first asks the subscriber whether any pause-guarded
        # subsystem is currently unhealthy. None = legacy behavior (no
        # upstream gate).
        self._evaluator_subscriber = evaluator_subscriber
        # P2.6-EXT (#123): caller can inject a custom policy map (e.g. loaded
        # from env/config at startup) to tune strict/lax per subsystem without
        # changing module code. Falls back to the module-level default.
        self._pause_policy: dict[str, str] = (
            pause_policy if pause_policy is not None else _PAUSE_GUARD_POLICY
        )

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
        # P2.6 (#597, FR45) + P2.6-EXT (#123): pause-on-unhealthy-upstream check
        # runs FIRST so we never burn arbitration state on a signal we're about
        # to suppress. Policy is per-subsystem: strict = suppress; lax = warn only.
        if self._evaluator_subscriber is not None:
            for guarded in _PAUSE_GUARD_SUBSYSTEMS:
                if self._evaluator_subscriber.is_paused(guarded):
                    policy = self._pause_policy.get(guarded, "strict")
                    if policy not in ("strict", "lax"):
                        logger.warning(
                            f"ARBITER_POLICY_UNKNOWN: unrecognised policy {policy!r} for "
                            f"'{guarded}', defaulting to strict",
                            extra={"correlation_id": correlation_id},
                        )
                        policy = "strict"
                    if policy == "strict":
                        reason = (
                            f"ARBITER_PAUSED: upstream '{guarded}' evaluator unhealthy — "
                            f"suppressing {symbol} {action} from {strategy_id}"
                        )
                        logger.info(reason, extra={"correlation_id": correlation_id})
                        return False, reason
                    else:
                        logger.warning(
                            f"ARBITER_WARN: upstream '{guarded}' evaluator unhealthy "
                            f"(lax policy) — allowing {symbol} {action} from "
                            f"{strategy_id} with degraded confidence",
                            extra={"correlation_id": correlation_id},
                        )

        # Guard: missing symbol or action means we cannot key arbitration state reliably.
        if not symbol or not action:
            reason = (
                f"ARBITER_BYPASS: missing symbol ({symbol!r}) or action ({action!r}) — "
                f"skipping arbitration for strategy={strategy_id}"
            )
            logger.warning(reason, extra={"correlation_id": correlation_id})
            return True, reason

        canonical_action = _normalise_action(action)

        # 1. Deduplication guard (60 s window, same symbol + canonical action)
        dedup_key = f"arbiter:dedup:{symbol}:{canonical_action}"
        if await self._cache.get(dedup_key):
            reason = (
                f"SIGNAL_DEDUPLICATED: {symbol} {canonical_action} already published "
                f"within the last {_DEDUP_TTL_SECONDS}s (strategy={strategy_id})"
            )
            logger.info(reason, extra={"correlation_id": correlation_id})
            return False, reason

        # 2. Conflict detection (5 min window, opposing action for same symbol)
        bias_key = f"arbiter:bias:{symbol}"
        raw_bias = await self._cache.get(bias_key)
        stored_action: str | None = None
        stored_conf: float = 0.0
        stored_strategy: str = "unknown"

        if raw_bias:
            try:
                stored_action, stored_conf_str, stored_strategy = raw_bias.split(":", 2)
                stored_conf = float(stored_conf_str)
            except (ValueError, AttributeError):
                logger.warning(
                    f"ARBITER_BIAS_CORRUPT: malformed bias value for {symbol}: {raw_bias!r}. "
                    "Resetting bias and allowing signal through.",
                    extra={"correlation_id": correlation_id},
                )
                await self._cache.set(bias_key, "", ttl=1)  # expire immediately
                stored_action = None

        if stored_action and stored_action != canonical_action:
            # Opposing signal detected within the conflict window
            if confidence <= stored_conf:
                reason = (
                    f"signal_conflict_resolved: {symbol} {canonical_action} from "
                    f"{strategy_id} (confidence={confidence:.3f}) suppressed in favour "
                    f"of {stored_action} from {stored_strategy} "
                    f"(confidence={stored_conf:.3f})"
                )
                logger.info(reason, extra={"correlation_id": correlation_id})
                return False, reason
            else:
                # Incoming signal wins — overwrite stored bias
                logger.info(
                    f"signal_conflict_resolved: {symbol} {canonical_action} from "
                    f"{strategy_id} (confidence={confidence:.3f}) wins over "
                    f"{stored_action} from {stored_strategy} "
                    f"(confidence={stored_conf:.3f})",
                    extra={"correlation_id": correlation_id},
                )

        # 3. Signal is allowed — record state in Redis
        bias_value = f"{canonical_action}:{confidence:.6f}:{strategy_id}"
        await self._cache.set(bias_key, bias_value, ttl=_CONFLICT_TTL_SECONDS)
        await self._cache.set(dedup_key, "1", ttl=_DEDUP_TTL_SECONDS)

        return True, "allowed"
