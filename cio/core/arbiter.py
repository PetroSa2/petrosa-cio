"""Signal arbitration layer for cross-strategy deduplication and conflict resolution."""

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from cio.core.cache import AsyncRedisCache, CacheUnavailable
from cio.core.evaluator_subscriber import PAUSE_GUARD_SUBSYSTEMS
from cio.core.metrics import meter
from cio.models import TriggerType

if TYPE_CHECKING:
    from cio.core.context_builder import ContextBuilder
    from cio.core.evaluator_subscriber import EvaluatorSubscriber
    from cio.core.position_review_loop import PositionKey
    from cio.core.router import OutputRouter
    from cio.models import DecisionResult, TriggerContext

logger = logging.getLogger(__name__)


class _ScheduledReviewEnforcer(Protocol):
    """Structural protocol matching `NurseEnforcer.audit` — avoids a hard
    import dependency from this module onto `cio.apps.nurse.enforcer`."""

    async def audit(self, context: "TriggerContext") -> "DecisionResult": ...


_DEDUP_TTL_SECONDS = 60

# petrosa-cio#406: Redis unreachable (after the client's bounded retry) during arbitration. outcome=refused is a
# new entry refused; outcome=passed_closing is a close or reduce-only intent let through without arbitration.
ARBITER_STATE_UNAVAILABLE = meter.create_counter(
    "cio_arbiter_state_unavailable_total",
    description="Signals arbitrated while Redis was unreachable, by outcome (refused | passed_closing)",
)
_CONFLICT_TTL_SECONDS = 300  # 5 minutes

# P2.6 (#597) + P2.6-EXT (#123): subsystems whose unhealthy verdict triggers
# pause behavior on every incoming signal. `ingest` was the first wired source
# (#593); execution, strategy-fidelity, and audit added by #123 (FR45 → GREEN).
_PAUSE_GUARD_SUBSYSTEMS: tuple[str, ...] = PAUSE_GUARD_SUBSYSTEMS

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


# Labelled fallback; override with CIO_ARBITER_BREAKER_SECONDS.
_ARBITER_BREAKER_FALLBACK_SECONDS = 5.0


def _breaker_seconds_from_env() -> float:
    """Return the labelled Redis breaker window, with a safe fallback for invalid values."""
    try:
        value = float(
            os.getenv(
                "CIO_ARBITER_BREAKER_SECONDS",
                str(_ARBITER_BREAKER_FALLBACK_SECONDS),
            )
        )
    except ValueError:
        return _ARBITER_BREAKER_FALLBACK_SECONDS
    return value if value >= 0 else _ARBITER_BREAKER_FALLBACK_SECONDS


CIO_ARBITER_BREAKER_SECONDS = _breaker_seconds_from_env()
CIO_ARBITER_BREAKER_OPEN_TOTAL = meter.create_counter(
    "cio_arbiter_breaker_open_total",
    description="Number of times the Redis arbiter circuit breaker opens",
)


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
        context_builder: "ContextBuilder | None" = None,
        enforcer: "_ScheduledReviewEnforcer | None" = None,
        router: "OutputRouter | None" = None,
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
        # #175 (FR60/P1.4-AC7) — optional collaborators wiring this arbiter
        # as the runner for `PositionReviewLoop` (see
        # `run_scheduled_review` below and the intended-wiring docstring on
        # `PositionReviewLoop`). All three are only present when the
        # process actually starts the in-position re-evaluation loop; None
        # = the loop is not running (or was constructed without them),
        # which `run_scheduled_review` treats as a no-op with a warning
        # rather than crashing the cadence task.
        self._context_builder = context_builder
        self._enforcer = enforcer
        self._router = router

        self._breaker_open_until: float | None = None
        self._breaker_seconds = CIO_ARBITER_BREAKER_SECONDS
        self._clock: Callable[[], float] = time.monotonic
        # Keep the half-open probe single-flight across concurrent callbacks.
        self._breaker_lock = asyncio.Lock()

    def _open_breaker(self, now: float) -> None:
        self._breaker_open_until = now + self._breaker_seconds
        CIO_ARBITER_BREAKER_OPEN_TOTAL.add(1)

    def _breaker_unavailable(
        self,
        symbol: str,
        canonical_action: str,
        strategy_id: str,
        correlation_id: str,
        closing: bool,
    ) -> tuple[bool, str]:
        if closing:
            ARBITER_STATE_UNAVAILABLE.add(1, {"outcome": "passed_closing"})
            reason = (
                f"ARBITER_STATE_UNAVAILABLE: circuit breaker open; closing intent {symbol} "
                f"{canonical_action} from {strategy_id} passes without arbitration"
            )
            logger.warning(reason, extra={"correlation_id": correlation_id})
            return True, reason
        ARBITER_STATE_UNAVAILABLE.add(1, {"outcome": "refused"})
        reason = (
            f"ARBITER_STATE_UNAVAILABLE: circuit breaker open; new entry {symbol} "
            f"{canonical_action} from {strategy_id} refused until arbitration state is readable "
            "(closes and reduce-only orders pass)"
        )
        logger.warning(reason, extra={"correlation_id": correlation_id})
        return False, reason

    async def run_scheduled_review(self, key: "PositionKey", reason: str) -> None:
        """Runner callback for `PositionReviewLoop` (P1.4-AC7, #135/#175).

        Fires a full `SCHEDULED_REVIEW` reasoning pass for an in-position
        re-evaluation: builds a fresh `TriggerContext` (COLD path — see
        `ContextBuilder.COLD_TRIGGERS`), runs it through the same
        enforcer/orchestrator pipeline as a live trade intent, and routes
        the resulting `DecisionResult` exactly like `NATSListener` does.
        This is the callback the `PositionReviewLoop` docstring names —
        without it the loop had nothing to call (#175).

        Never raises: `PositionReviewLoop._fire_once` already guards
        runner exceptions, but this method adds its own try/except so a
        partial failure (e.g. context_builder HTTP error) is logged with
        the position key for operator triage instead of surfacing as a
        generic "runner_failed" line.
        """
        if (
            self._context_builder is None
            or self._enforcer is None
            or self._router is None
        ):
            logger.warning(
                "SCHEDULED_REVIEW skipped for %s (reason=%s): run_scheduled_review "
                "collaborators not wired (context_builder=%s enforcer=%s router=%s)",
                key,
                reason,
                self._context_builder is not None,
                self._enforcer is not None,
                self._router is not None,
            )
            return

        correlation_id = f"scheduled-review-{key}-{uuid.uuid4().hex[:8]}"
        payload = {
            "strategy_id": key.strategy_id,
            "position_id": key.position_id,
            "reeval_reason": reason,
        }
        try:
            context = await self._context_builder.build(
                correlation_id=correlation_id,
                source_subject="cio.position_review_loop",
                trigger_type=TriggerType.SCHEDULED_REVIEW,
                payload=payload,
            )
            decision = await self._enforcer.audit(context)
            await self._router.route(context, decision)
            logger.info(
                "scheduled_review.dispatched position=%s reason=%s action=%s",
                key,
                reason,
                getattr(decision.action, "value", decision.action),
                extra={"correlation_id": correlation_id},
            )
        except Exception as exc:  # noqa: BLE001 — never crash the cadence loop
            logger.warning(
                "scheduled_review.failed position=%s reason=%s error=%s",
                key,
                reason,
                exc,
                extra={"correlation_id": correlation_id},
            )

    async def check(
        self,
        symbol: str,
        action: str,
        confidence: float,
        strategy_id: str,
        correlation_id: str,
        closing: bool = False,
    ) -> tuple[bool, str]:
        """
        Check whether the incoming signal should be allowed through.

        ``closing`` marks a close or reduce-only intent (``drawdown.is_closing_intent``). It only matters when the
        arbitration state in Redis can't be read: new entries are then refused, closes still pass (#406).

        Returns:
            (allowed, reason) — ``allowed=False`` means the signal must be suppressed.
        """
        # P2.6 (#597, FR45) + P2.6-EXT (#123): pause-on-unhealthy-upstream check
        # runs FIRST so we never burn arbitration state on a signal we're about
        # to suppress. Policy is per-subsystem: strict = suppress; lax = warn only.
        if self._evaluator_subscriber is not None:
            for guarded in _PAUSE_GUARD_SUBSYSTEMS:
                # #193: pass the incoming signal's symbol so a fault the publisher
                # scoped to a different symbol does not halt decisioning on this one.
                # When the publisher sent no scope, is_paused stays conservative.
                if self._evaluator_subscriber.is_paused(guarded, symbol=symbol):
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
        async with self._breaker_lock:
            now = self._clock()
            if self._breaker_open_until is not None:
                if now < self._breaker_open_until:
                    return self._breaker_unavailable(
                        symbol, canonical_action, strategy_id, correlation_id, closing
                    )
                self._breaker_open_until = None

            # 1. Deduplication guard (60 s window, same symbol + canonical action)
            dedup_key = f"arbiter:dedup:{symbol}:{canonical_action}"
            bias_key = f"arbiter:bias:{symbol}"
            try:
                dedup_hit = await self._cache.get_strict(dedup_key)
                raw_bias = None if dedup_hit else await self._cache.get_strict(bias_key)
            except CacheUnavailable as exc:
                self._open_breaker(now)
                return self._state_unavailable(
                    symbol, canonical_action, strategy_id, correlation_id, closing, exc
                )

        if dedup_hit:
            reason = (
                f"SIGNAL_DEDUPLICATED: {symbol} {canonical_action} already published "
                f"within the last {_DEDUP_TTL_SECONDS}s (strategy={strategy_id})"
            )
            logger.info(reason, extra={"correlation_id": correlation_id})
            return False, reason

        # 2. Conflict detection (5 min window, opposing action for same symbol)
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
                    "Resetting bias and allowing the incoming signal through.",
                    extra={"correlation_id": correlation_id},
                )
                # Expire malformed arbitration state before recording a fresh bias.
                await self._cache.set(bias_key, "", ttl=1)
                stored_action = None

        if stored_action and stored_action != canonical_action:
            # Opposing signal detected within the conflict window.
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
                # Incoming signal wins and replaces the stored bias.
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

    def _state_unavailable(
        self,
        symbol: str,
        canonical_action: str,
        strategy_id: str,
        correlation_id: str,
        closing: bool,
        exc: Exception,
    ) -> tuple[bool, str]:
        """petrosa-cio#406: without the dedup and conflict state, a new entry fails closed, like an unknown
        drawdown (#398/#405). An absent key would have let a duplicate or a losing opposing signal through, so an
        error must never read as one. Closes and reduce-only intents still pass: they only reduce exposure."""
        if closing:
            ARBITER_STATE_UNAVAILABLE.add(1, {"outcome": "passed_closing"})
            reason = (
                f"ARBITER_STATE_UNAVAILABLE: Redis unreachable ({exc}); closing intent {symbol} "
                f"{canonical_action} from {strategy_id} passes without arbitration"
            )
            logger.warning(reason, extra={"correlation_id": correlation_id})
            return True, reason
        ARBITER_STATE_UNAVAILABLE.add(1, {"outcome": "refused"})
        reason = (
            f"ARBITER_STATE_UNAVAILABLE: Redis unreachable ({exc}); new entry {symbol} {canonical_action} "
            f"from {strategy_id} refused until arbitration state is readable "
            "(closes and reduce-only orders pass)"
        )
        logger.warning(reason, extra={"correlation_id": correlation_id})
        return False, reason
