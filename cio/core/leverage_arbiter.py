"""Admission-time leverage arbitration (P1.5-AC3, FR61).

Pure function + small helper for resolving the leverage CIO will send
downstream to the trade engine on every admission decision. The three
inputs are intentionally optional so the arbiter is forward-compatible
with the still-open enrichment sources:

- ``recommended_leverage``  — the strategy's preference, surfaced by
  the signal pipeline once `691.1` ships. Treated as ``None`` today.
- ``strategy_envelope``     — the per-strategy max from the most recent
  characterization, surfaced once `petrosa-data-manager#179` ships its
  typed field. Treated as ``None`` today.
- ``operator_max``          — the env-var fallback. Always present:
  ``CIO_DEFAULT_MAX_LEVERAGE`` (default 10).

Until the enrichment fields land, every admission resolves to
``operator_max`` — the documented fallback path, not a regression. When
they land the call site passes them in and the arbiter logic is
unchanged.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


DEFAULT_OPERATOR_MAX_LEVERAGE = 10


def operator_max_from_env() -> int:
    """Resolve the operator-configured ceiling from ``CIO_DEFAULT_MAX_LEVERAGE``."""
    raw = os.getenv("CIO_DEFAULT_MAX_LEVERAGE")
    if raw is None or raw == "":
        return DEFAULT_OPERATOR_MAX_LEVERAGE
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid CIO_DEFAULT_MAX_LEVERAGE=%r — falling back to default=%s",
            raw,
            DEFAULT_OPERATOR_MAX_LEVERAGE,
        )
        return DEFAULT_OPERATOR_MAX_LEVERAGE
    if value < 1:
        logger.warning("CIO_DEFAULT_MAX_LEVERAGE=%s is below 1 — clamping to 1", value)
        return 1
    return value


@dataclass(frozen=True)
class LeverageDecision:
    """Outcome of one admission-time arbitration.

    Attributes:
        decided_leverage: the integer leverage the CIO will send downstream.
        per_strategy_bound: the effective bound the arbiter resolved against
            (``min(operator_max, strategy_envelope)`` when the envelope is
            present, otherwise ``operator_max``).
        branch: one of ``accept`` / ``override`` / ``fallback``. ``accept``
            means the recommendation was inside the bound; ``override`` means
            it was over the bound (no rejection — clamp to the bound); and
            ``fallback`` means the recommendation was absent and we used the
            bound directly.
        audit_reason: human-readable trace of why ``decided_leverage`` came
            out the way it did. Carried into the audit-trail row.
    """

    decided_leverage: int
    per_strategy_bound: int
    branch: str
    audit_reason: str


def arbitrate_leverage(
    *,
    recommended_leverage: int | None,
    operator_max: int | None = None,
    strategy_envelope: int | None = None,
) -> LeverageDecision:
    """Resolve the admission-time leverage decision.

    Branches (per AC3.b):

    - ``recommended_leverage is None`` → ``decided = per_strategy_bound``
      (``min(operator_max, strategy_envelope)`` when the envelope is
      present; ``operator_max`` otherwise). Branch ``fallback``.
    - ``recommended_leverage <= per_strategy_bound`` → ``decided =
      recommended_leverage``. Branch ``accept``.
    - ``recommended_leverage >  per_strategy_bound`` → ``decided =
      per_strategy_bound`` (clamped; no rejection). Branch ``override``.

    ``operator_max=None`` is interpreted as "no caller-supplied value;
    pick up the env var now." This keeps the function callable without
    pre-computing the env-var lookup at every site.
    """
    effective_operator_max = (
        operator_max if operator_max is not None else operator_max_from_env()
    )
    if effective_operator_max < 1:
        # Defensive — operator_max_from_env clamps to 1, but explicit
        # callers might pass 0/negative. Treat sub-1 as 1.
        effective_operator_max = 1

    if strategy_envelope is not None and strategy_envelope >= 1:
        per_strategy_bound = min(effective_operator_max, strategy_envelope)
    else:
        per_strategy_bound = effective_operator_max

    if recommended_leverage is None:
        return LeverageDecision(
            decided_leverage=per_strategy_bound,
            per_strategy_bound=per_strategy_bound,
            branch="fallback",
            audit_reason=(
                "no recommended_leverage on signal — applied "
                f"per-strategy bound={per_strategy_bound} "
                f"(operator_max={effective_operator_max}, "
                f"strategy_envelope={strategy_envelope})"
            ),
        )

    if recommended_leverage < 1:
        # A non-positive recommendation is treated as "no recommendation"
        # — defensive against upstream bugs / null-coalescing accidents.
        logger.warning(
            "arbitrate_leverage received recommended_leverage=%s (<1); "
            "treating as no-recommendation",
            recommended_leverage,
        )
        return LeverageDecision(
            decided_leverage=per_strategy_bound,
            per_strategy_bound=per_strategy_bound,
            branch="fallback",
            audit_reason=(
                f"recommended_leverage={recommended_leverage} below 1 — "
                "treated as missing; "
                f"applied per-strategy bound={per_strategy_bound}"
            ),
        )

    if recommended_leverage <= per_strategy_bound:
        return LeverageDecision(
            decided_leverage=recommended_leverage,
            per_strategy_bound=per_strategy_bound,
            branch="accept",
            audit_reason=(
                f"recommended_leverage={recommended_leverage} "
                f"within per-strategy bound={per_strategy_bound} — accepted as-is"
            ),
        )

    return LeverageDecision(
        decided_leverage=per_strategy_bound,
        per_strategy_bound=per_strategy_bound,
        branch="override",
        audit_reason=(
            f"recommended_leverage={recommended_leverage} above "
            f"per-strategy bound={per_strategy_bound} — overridden to "
            f"{per_strategy_bound} (no rejection per AC3.b)"
        ),
    )
