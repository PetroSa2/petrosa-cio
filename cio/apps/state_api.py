"""CIO /state endpoint — operator view of evaluator-driven pauses (P2.6).

GET  /state                       — full evaluator snapshot + paused list
GET  /state/paused                — short paused-subsystems list
POST /state/override/{subsystem}  — set or clear an operator pause override

The store this reads from is the in-process EvaluatorSubscriber attached
to ``app.state.evaluator_subscriber`` at startup. When the subscriber
isn't wired (e.g. local dev without NATS), every read returns an empty
snapshot and the override endpoint reports the missing dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/state", tags=["state"])


class OverrideBody(BaseModel):
    """Body for the override POST. ``verdict=null`` clears the override."""

    verdict: str | None = None


def _subscriber(request: Request):
    sub = getattr(request.app.state, "evaluator_subscriber", None)
    if sub is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "evaluator subscriber is not wired — CIO is running without "
                "the P2.6 pause gate"
            ),
        )
    return sub


@router.get("")
async def get_state(request: Request) -> dict:
    sub = _subscriber(request)
    return sub.snapshot()


@router.get("/paused")
async def get_paused(request: Request) -> dict:
    sub = _subscriber(request)
    return {"paused": sub.paused_subsystems()}


@router.post("/override/{subsystem}")
async def set_override(subsystem: str, body: OverrideBody, request: Request) -> dict:
    sub = _subscriber(request)
    try:
        sub.set_override(subsystem, body.verdict)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"subsystem": subsystem, "override": body.verdict}
