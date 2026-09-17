"""Context-completeness gate (petrosa-cio#199).

The reasoning loop previously ran the LLM personas even when every
pre-decision context surface (market / strategy_stats / strategy_defaults)
had timed out against data-manager. With zero real context, the LLM's
only rational output is ``pause_strategy`` — so a single upstream outage
(a `CONTEXT_FETCH_TIMEOUT_STORM`, see `ContextBuilder`) turned into a
self-sustaining pause loop that never recovered on its own, even after
data-manager came back healthy, because nothing re-evaluated the frozen
state (`PAUSE_SKIPPED: already frozen` fired forever).

This module owns two cooperating pieces:

  1. :func:`apply_context_gate` — called by :class:`~cio.core.orchestrator.
     Orchestrator` before *any* LLM persona (or even `CodeEngine`) runs.
     When >= ``CIO_CONTEXT_GATE_MIN_SURFACES`` (default 2) context
     surfaces recorded a ``read_timeout`` gap in this cycle, the LLM is
     never invoked; the cycle emits a ``CONTEXT_UNAVAILABLE`` verdict
     (:func:`build_context_unavailable_result`) and is non-authoritative
     — the caller must hold the previous state rather than treat this as
     a fresh directive.
  2. Idempotent + reversible freeze bookkeeping: when the gate trips, the
     strategy's existing ``cio:freeze:<id>`` Redis key (owned by
     `OutputRouter`, see router.py) is (re)marked with a distinct
     ``LOCKED:CONTEXT_UNAVAILABLE`` value — never clobbering a genuine
     LLM-decided ``pause_strategy`` freeze, which keeps the plain
     ``LOCKED`` value. Once context has been healthy (gate not tripped)
     for ``CIO_CONTEXT_GATE_UNFREEZE_STREAK`` (default 3) *consecutive*
     cycles, the freeze is cleared immediately rather than waiting out
     its TTL — the strategy is reconsidered on the very next signal
     instead of staying frozen on stale grounds.

Both thresholds are env-overridable so a noisy environment (e.g. a
data-manager rollout) can be tuned without a code change.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from cio.models.decision import DecisionResult
from cio.models.enums import (
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    HealthStatus,
    RegimeFit,
    RejectionSource,
)

if TYPE_CHECKING:
    from cio.core.cache import AsyncRedisCache
    from cio.models.context import PreDecisionContext, TriggerContext

logger = logging.getLogger(__name__)

FREEZE_KEY_PREFIX = "cio:freeze:"
HEALTHY_STREAK_KEY_PREFIX = "cio:context_healthy_streak:"

# Distinct from the plain "LOCKED" value `OutputRouter` writes for a
# genuine LLM `pause_strategy` decision (router.py `_apply_rate_limit_freeze`
# / the AC3 (cio#169) pause-freeze block) — this marker is what makes the
# freeze auto-unfreeze *reversible*: the gate only ever clears a freeze it
# recognizes as its own.
CONTEXT_UNAVAILABLE_FREEZE_VALUE = "LOCKED:CONTEXT_UNAVAILABLE"

_DEFAULT_MIN_SURFACES = 2
_DEFAULT_UNFREEZE_STREAK = 3
_DEFAULT_FREEZE_TTL_S = 1800


def _min_surfaces() -> int:
    return int(os.getenv("CIO_CONTEXT_GATE_MIN_SURFACES", str(_DEFAULT_MIN_SURFACES)))


def _unfreeze_streak() -> int:
    return int(
        os.getenv("CIO_CONTEXT_GATE_UNFREEZE_STREAK", str(_DEFAULT_UNFREEZE_STREAK))
    )


def _freeze_ttl_s() -> int:
    return int(os.getenv("CIO_CONTEXT_GATE_FREEZE_TTL_S", str(_DEFAULT_FREEZE_TTL_S)))


def count_timeout_gaps(pre_decision_context: PreDecisionContext | None) -> int:
    """Count surfaces that fell back to defaults because of a ReadTimeout.

    Mirrors ``ContextBuilder._log_timeout_storm_if_concurrent``'s own
    ``reason.startswith("read_timeout")`` filter so the gate agrees with
    the ``CONTEXT_FETCH_TIMEOUT_STORM`` log line about what counts as a
    surface being "down" this cycle. Returns 0 (never trips) when the
    caller did not assemble a bundle — legacy call sites without
    ``pre_decision_context`` wired are unaffected.
    """
    if pre_decision_context is None:
        return 0
    return sum(
        1 for gap in pre_decision_context.gaps if gap.reason.startswith("read_timeout")
    )


def build_context_unavailable_result(
    *, surfaces_down: int, correlation_id: str
) -> DecisionResult:
    """The non-authoritative verdict emitted when the gate trips (AC1).

    ``action=SKIP`` reuses the existing "do nothing this cycle"
    vocabulary — callers (the router / dispatch layer) must not treat
    this as a fresh directive. In particular it must never itself set or
    clear a freeze; :func:`apply_context_gate` owns that side-effect
    separately so `SKIP` stays a pure no-op everywhere else it is used.
    """
    reason = (
        f"CONTEXT_UNAVAILABLE: {surfaces_down} context surface(s) timed out "
        "concurrently this cycle; holding previous state instead of "
        "deciding on empty/degraded context."
    )
    logger.error(
        "CONTEXT_UNAVAILABLE: skipping LLM invocation — %d surfaces down "
        "correlation_id=%s",
        surfaces_down,
        correlation_id,
        extra={"correlation_id": correlation_id, "surfaces_down": surfaces_down},
    )
    return DecisionResult(
        hard_blocked=False,
        ev_passes=False,
        cost_viable=False,
        regime_confidence=ConfidenceLevel.LOW,
        regime_fit=RegimeFit.NEUTRAL,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        action=ActionType.SKIP,
        justification=reason,
        thought_trace="CONTEXT_UNAVAILABLE",
        rejection_source=RejectionSource.CONTEXT_UNAVAILABLE,
    )


async def apply_context_gate(
    context: TriggerContext, cache: AsyncRedisCache | None
) -> DecisionResult | None:
    """Evaluate + (when wired to a cache) act on the context-completeness gate.

    Returns a ``CONTEXT_UNAVAILABLE`` :class:`DecisionResult` when the
    gate trips — the caller (``Orchestrator.run``) MUST return it
    immediately without invoking CodeEngine or any LLM persona. Returns
    ``None`` when the gate does not trip, meaning the caller proceeds
    with its normal reasoning-loop flow.

    ``cache=None`` (no Redis wired, e.g. most unit tests) still gates the
    LLM invocation correctly; only the freeze/auto-unfreeze bookkeeping
    is skipped in that case.
    """
    surfaces_down = count_timeout_gaps(context.pre_decision_context)
    strategy_id = context.strategy_id
    freeze_key = f"{FREEZE_KEY_PREFIX}{strategy_id}"
    streak_key = f"{HEALTHY_STREAK_KEY_PREFIX}{strategy_id}"

    if surfaces_down >= _min_surfaces():
        if cache is not None:
            current = await cache.get(freeze_key)
            # Never clobber a genuine LLM-decided pause_strategy freeze
            # (plain "LOCKED..." set by router.py) — only (re)apply our
            # own marker when the slot is empty or already ours.
            if current is None or current == CONTEXT_UNAVAILABLE_FREEZE_VALUE:
                await cache.set(
                    freeze_key, CONTEXT_UNAVAILABLE_FREEZE_VALUE, ttl=_freeze_ttl_s()
                )
            # A fresh trip resets any in-progress healthy streak.
            await cache.delete(streak_key)
        return build_context_unavailable_result(
            surfaces_down=surfaces_down, correlation_id=context.correlation_id
        )

    if cache is not None:
        current = await cache.get(freeze_key)
        if current == CONTEXT_UNAVAILABLE_FREEZE_VALUE:
            raw_streak = await cache.get(streak_key)
            streak = (
                int(raw_streak) + 1
                if raw_streak is not None and raw_streak.isdigit()
                else 1
            )
            if streak >= _unfreeze_streak():
                await cache.delete(freeze_key)
                await cache.delete(streak_key)
                logger.info(
                    "CONTEXT_GATE_AUTO_UNFREEZE: strategy_id=%s healthy for "
                    "%d consecutive cycles — clearing stale "
                    "CONTEXT_UNAVAILABLE freeze early (TTL not yet expired)",
                    strategy_id,
                    streak,
                )
            else:
                await cache.set(streak_key, str(streak), ttl=_freeze_ttl_s())
        elif current is None:
            # No freeze at all (either never tripped, or a genuine
            # pause_strategy freeze already resolved) — nothing to
            # track. Clear any orphaned streak counter left over from a
            # gate trip whose freeze already expired via TTL.
            await cache.delete(streak_key)
    return None
