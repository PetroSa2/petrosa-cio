"""CIO /api/dashboard routes for the operator dashboard SPA (P5.1a follow-up, #654).

GET  /api/dashboard/decisions/recent?window=24h[&strategy_id=...]
     Newest-first CIO decision feed with reasoning trace.

GET  /api/dashboard/evaluator/verdicts[?subsystem=<name>]
     Current verdict for all 8 subsystems (or one when filtered).

Both routes use RFC 7807-shaped problem JSON for errors, matching the
petrosa-data-manager /api/dashboard/ contract (PR #159, merge 5df8080).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_UTC = UTC

_WINDOW_MAP: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _resolve_window(window: str) -> timedelta:
    td = _WINDOW_MAP.get(window)
    if td is None:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "about:blank",
                "title": "Invalid window parameter",
                "status": 400,
                "detail": f"window must be one of {sorted(_WINDOW_MAP)}, got {window!r}",
            },
        )
    return td


def _problem(status: int, title: str, detail: str) -> dict:
    return {"type": "about:blank", "title": title, "status": status, "detail": detail}


@router.get("/decisions/recent")
async def get_decisions_recent(
    request: Request,
    window: str = Query(default="24h"),
    strategy_id: str | None = Query(default=None),
) -> dict:
    """Newest-first CIO decision feed with reasoning trace."""
    store = getattr(request.app.state, "decision_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=_problem(
                503,
                "Decision store unavailable",
                "decision_store is not wired — CIO is running without a decision store",
            ),
        )
    td = _resolve_window(window)
    since = datetime.now(_UTC) - td
    records = store.recent(since, strategy_id=strategy_id)
    return {
        "window": window,
        "strategy_id": strategy_id,
        "decisions": [
            {
                "decision_id": r.decision_id,
                "strategy_id": r.strategy_id,
                "action": r.action,
                "reasoning_trace": r.reasoning_trace,
                "confidence": r.confidence,
                "timestamp": r.timestamp.isoformat(),
                # FR53 / P3.4 (#130): refusal taxonomy + revision drift visibility.
                "rejection_source": r.rejection_source,
                "strategy_revision_id": r.strategy_revision_id,
            }
            for r in records
        ],
    }


@router.get("/llm-spend")
async def get_llm_spend() -> dict:
    """Current-period LLM spend bucketed by CIO decision type (FR63).

    Returns the spend accumulated since midnight UTC, projected daily total,
    per-day ceiling, and distance-to-ceiling so the operator dashboard SPA
    can render the LlmSpendPane (AC1/AC2 of petrosa-data-manager#170).
    """
    from cio.core.spend_tracker import LlmSpendTracker

    return LlmSpendTracker.instance().period_snapshot()


@router.get("/evaluator/verdicts")
async def get_evaluator_verdicts(
    request: Request,
    subsystem: str | None = Query(default=None),
) -> dict:
    """Current verdict for all 8 subsystems (or one when filtered)."""
    sub = getattr(request.app.state, "evaluator_subscriber", None)
    if sub is None:
        raise HTTPException(
            status_code=503,
            detail=_problem(
                503,
                "Evaluator subscriber unavailable",
                "evaluator_subscriber is not wired — CIO is running without the P2.6 pause gate",
            ),
        )
    verdicts = []
    for key, (verdict, reason, observed_at) in sub._verdicts.items():
        if subsystem and key != subsystem:
            continue
        verdicts.append(
            {
                "subsystem": key,
                "verdict": verdict,
                "last_tick_at": observed_at.isoformat(),
                "evidence": reason,
            }
        )
    if subsystem and not verdicts:
        raise HTTPException(
            status_code=404,
            detail=_problem(
                404,
                "Subsystem not found",
                f"No verdict recorded for subsystem {subsystem!r}",
            ),
        )
    return {"subsystems": verdicts}
