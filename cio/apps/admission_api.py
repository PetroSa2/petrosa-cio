"""``POST /api/admission/register`` HTTP surface for ``PortfolioTracker.record_admit``
(petrosa-cio#156, FR54-B precursor).

Background — the cio admission flow lived entirely in-process as
:meth:`cio.core.portfolio_tracker.PortfolioTracker.record_admit`. The FR54
self-service strategy pipeline ([petrosa-bot-ta-analysis#256](https://github.com/PetroSa2/petrosa-bot-ta-analysis/issues/256))
needs to register a characterized strategy with cio over HTTP from the CLI
host (which doesn't share the cio Python process). This module wraps
``record_admit`` behind a FastAPI route so the CLI (and any other
out-of-process caller) can drive admission without taking a Python
dependency on the cio package.

The route reads the tracker from ``app.state.portfolio_tracker`` (the same
``app.state``-attached convention the rest of cio's apps follow — see
``cio/apps/state_api.py``). If the tracker isn't wired (local-dev mode),
the route returns ``503``.

**Important — no admission policy is enforced here.** The
``PortfolioTracker.would_breach_ceiling`` check is the orchestrator's
concern; this route is a thin record/replace pass-through (matching the
existing in-process call shape from ``cio.core.orchestrator``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/admission", tags=["admission"])


class AdmissionRegisterRequest(BaseModel):
    strategy_id: str = Field(
        ..., min_length=1, description="Stable strategy identifier"
    )
    position_size_usd: float = Field(
        ..., ge=0.0, description="Admitted notional in USD (>= 0)"
    )
    leverage: float = Field(..., ge=1.0, description="Admitted leverage (>= 1)")
    # Audit-trail fields, passed through to logs/metrics only. Not stored in
    # the in-process tracker today (no schema change to the existing record).
    strategy_revision_id: str | None = Field(
        None, description="Optional FR53 strategy-revision id for audit trail"
    )
    submitted_by: str | None = Field(
        None, description="Operator handle for audit trail"
    )


@router.post(
    "/register",
    status_code=201,
    response_model=None,
)
async def register_admission(
    req: AdmissionRegisterRequest,
    request: Request,
) -> dict[str, Any]:
    tracker = getattr(request.app.state, "portfolio_tracker", None)
    if tracker is None:
        raise HTTPException(
            status_code=503,
            detail={
                "title": "PortfolioTracker not wired",
                "detail": (
                    "cio is in local-dev mode (no app.state.portfolio_tracker) — "
                    "admission cannot be recorded."
                ),
            },
        )
    await tracker.record_admit(
        strategy_id=req.strategy_id,
        position_size_usd=req.position_size_usd,
        leverage=req.leverage,
    )
    return {
        "strategy_id": req.strategy_id,
        "position_size_usd": req.position_size_usd,
        "leverage": req.leverage,
        "strategy_revision_id": req.strategy_revision_id,
        "submitted_by": req.submitted_by,
        "status": "admitted",
    }
