"""Portfolio aggregate-leverage tracker (P1.5-AC5, FR61, #138).

In-memory accounting of admitted positions so the CIO admission-step
can refuse new positions whose combined leverage exposure would breach
the operator-configured ceiling.

Aggregate definition (AC5.a):

    aggregate(equity) = Σ(position_size_usd × leverage) / equity

over the set of currently-tracked positions. The CIO rejects an
incoming admission when

    aggregate + new_position_contribution > ceiling

where ``new_position_contribution = new_size × new_leverage / equity``.

Position-exit wiring (``record_exit``) is intentionally exposed but not
yet driven from a TE event stream — that wire-up is a follow-up. Until
then the tracker reflects "what CIO has admitted" rather than "what
TE currently holds open". Documented limitation; the producer-side
admission check is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


DEFAULT_PORTFOLIO_LEVERAGE_CEILING = 5.0


def ceiling_from_env() -> float:
    """Resolve the operator-configured aggregate ceiling.

    Env var: ``CIO_PORTFOLIO_LEVERAGE_CEILING`` (float, default 5.0).
    Bad input falls back to the default; sub-zero values are clamped
    to zero (any non-zero admission would trip the ceiling, which is
    the conservative read of a misconfigured value).
    """
    raw = os.getenv("CIO_PORTFOLIO_LEVERAGE_CEILING")
    if raw is None or raw == "":
        return DEFAULT_PORTFOLIO_LEVERAGE_CEILING
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid CIO_PORTFOLIO_LEVERAGE_CEILING=%r — falling back to default=%s",
            raw,
            DEFAULT_PORTFOLIO_LEVERAGE_CEILING,
        )
        return DEFAULT_PORTFOLIO_LEVERAGE_CEILING
    if value < 0:
        logger.warning(
            "CIO_PORTFOLIO_LEVERAGE_CEILING=%s is below 0 — clamping to 0", value
        )
        return 0.0
    return value


@dataclass(frozen=True)
class CeilingCheckResult:
    """Outcome of one would-breach query against the tracker."""

    would_breach: bool
    current_aggregate: float
    projected_aggregate: float
    ceiling: float
    reason: str


@dataclass(frozen=True)
class _Position:
    """Per-strategy snapshot the tracker keeps in memory."""

    position_size_usd: float
    leverage: float


class PortfolioTracker:
    """Cross-strategy aggregate-leverage accountant.

    State is process-local. Callers re-create the tracker on pod restart
    — that resets exposure to zero and admissions resume from a clean
    slate. Acceptable trade-off for an MVP because the admission gate
    "fails open" on restart (no false rejects), and the next admission
    re-populates the ledger.

    Thread-safety: an `asyncio.Lock` serialises mutating ops. Reads
    (``compute_aggregate``, ``would_breach_ceiling``) acquire the lock
    too so they see a consistent point-in-time view.
    """

    def __init__(self) -> None:
        self._positions: dict[str, _Position] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Mutators

    async def record_admit(
        self,
        *,
        strategy_id: str,
        position_size_usd: float,
        leverage: float,
    ) -> None:
        """Record (or replace) the strategy's currently-admitted position."""
        if not strategy_id:
            logger.warning("PortfolioTracker.record_admit refused empty strategy_id")
            return
        size = max(0.0, float(position_size_usd))
        lev = max(0.0, float(leverage))
        async with self._lock:
            self._positions[strategy_id] = _Position(
                position_size_usd=size,
                leverage=lev,
            )

    async def record_exit(self, *, strategy_id: str) -> None:
        """Drop a strategy from the active-positions set."""
        async with self._lock:
            self._positions.pop(strategy_id, None)

    # ------------------------------------------------------------------
    # Readers

    async def compute_aggregate(self, *, equity: float) -> float:
        """Return Σ(size × leverage) / equity across tracked positions.

        ``equity <= 0`` is conservative: returns ``+inf`` so any caller
        treating the value as "too high" rejects on the spot. (A pod
        that genuinely has zero equity should not be admitting new
        positions; this preserves that invariant without crashing.)
        """
        async with self._lock:
            return self._compute_aggregate_locked(equity=equity)

    async def would_breach_ceiling(
        self,
        *,
        new_position_size_usd: float,
        new_leverage: float,
        equity: float,
        ceiling: float | None = None,
    ) -> CeilingCheckResult:
        """Check whether admitting ``(size, leverage)`` would breach the ceiling.

        Excluding the strategy's prior admission (if any) would give a
        looser check, but per AC5.b the breach test is a strict
        "current + new" sum: a strategy upgrading its position still
        contributes its prior exposure until ``record_admit`` replaces
        it. The orchestrator calls this BEFORE ``record_admit``, so
        the answer reflects the cluster as it stands at admission time.
        """
        effective_ceiling = ceiling if ceiling is not None else ceiling_from_env()
        async with self._lock:
            current = self._compute_aggregate_locked(equity=equity)
            if equity <= 0:
                projected = float("inf")
            else:
                new_size = max(0.0, float(new_position_size_usd))
                new_lev = max(0.0, float(new_leverage))
                new_contribution = (new_size * new_lev) / equity
                projected = current + new_contribution
            breach = projected > effective_ceiling
            reason = (
                f"aggregate_leverage_ceiling check: "
                f"current={current:.4f} projected={projected:.4f} "
                f"ceiling={effective_ceiling:.4f} → "
                f"{'BREACH' if breach else 'OK'}"
            )
            return CeilingCheckResult(
                would_breach=breach,
                current_aggregate=current,
                projected_aggregate=projected,
                ceiling=effective_ceiling,
                reason=reason,
            )

    # ------------------------------------------------------------------
    # Test introspection (kept narrow)

    @property
    def tracked_strategy_count(self) -> int:
        return len(self._positions)

    # ------------------------------------------------------------------
    # Internals

    def _compute_aggregate_locked(self, *, equity: float) -> float:
        if equity <= 0:
            return float("inf")
        total = sum(p.position_size_usd * p.leverage for p in self._positions.values())
        return total / equity


portfolio_tracker = PortfolioTracker()
