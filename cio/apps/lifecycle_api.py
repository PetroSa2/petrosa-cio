"""FastAPI router for strategy lifecycle endpoints (P1.2, #114).

Exposes:
  POST /strategies/register        — register a new strategy definition (FR1, FR5)
  GET  /strategies/{sid}/lifecycle — operator view of the full transition
                                     history for one strategy (feeds FR9)
  GET  /strategies                 — list all registered strategy ids

The router is mounted by `cio.main` and reads/writes through a
`StrategyLifecycleStore` instance held on `app.state.lifecycle_store`. The
store is in-memory; persistence is intentionally pluggable so a future
Mongo- or Qdrant-backed implementation can drop in without changing the
endpoint contracts.

Per the P0.1 cross-service identifier contract, every transition (including
the genesis registration event) carries a `decision_id`. The registration
endpoint accepts an optional `decision_id` in the request body; if omitted
a new hex id is minted on the server side.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from cio.core.lifecycle import (
    InvalidTransition,
    LifecycleEvent,
    LifecycleState,
    StrategyAlreadyRegistered,
    StrategyLifecycleStore,
    StrategyNotRegistered,
)

router = APIRouter(prefix="/strategies", tags=["lifecycle"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegisterStrategyRequest(BaseModel):
    """Request body for `POST /strategies/register`.

    `definition` is intentionally an open dict — strategy definitions are
    owned by the publishing service (`petrosa-bot-ta-analysis` /
    `petrosa-realtime-strategies`). The CIO records them verbatim for
    audit and replay; field-level validation is the publisher's
    responsibility.
    """

    strategy_id: str = Field(..., min_length=1)
    definition: dict[str, Any] = Field(default_factory=dict)
    decision_id: str | None = Field(default=None)
    reasoning: dict[str, Any] = Field(default_factory=dict)


class LifecycleEventResponse(BaseModel):
    strategy_id: str
    from_state: LifecycleState | None
    to_state: LifecycleState
    action: str | None
    decision_id: str
    reasoning: dict[str, Any]
    at: datetime

    @classmethod
    def from_event(cls, event: LifecycleEvent) -> LifecycleEventResponse:
        return cls(
            strategy_id=event.strategy_id,
            from_state=event.from_state,
            to_state=event.to_state,
            action=event.action.value if event.action else None,
            decision_id=event.decision_id,
            reasoning=event.reasoning,
            at=event.at,
        )


class RegisterStrategyResponse(BaseModel):
    strategy_id: str
    current_state: LifecycleState
    event: LifecycleEventResponse


class LifecycleHistoryResponse(BaseModel):
    strategy_id: str
    current_state: LifecycleState
    history: list[LifecycleEventResponse]


class StrategyListResponse(BaseModel):
    strategy_ids: list[str]


# ---------------------------------------------------------------------------
# Store access
# ---------------------------------------------------------------------------


def _store(request: Request) -> StrategyLifecycleStore:
    """Resolve the lifecycle store from app state.

    `cio.main` is expected to attach an instance on
    `app.state.lifecycle_store` during startup. Tests can override via
    the FastAPI dependency-override mechanism by mounting their own
    `app.state.lifecycle_store` before sending requests.
    """
    store = getattr(request.app.state, "lifecycle_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="lifecycle store not initialized",
        )
    return store


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=RegisterStrategyResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_strategy(
    payload: RegisterStrategyRequest, request: Request
) -> RegisterStrategyResponse:
    store = _store(request)
    try:
        event = store.register(
            payload.strategy_id,
            payload.definition,
            decision_id=payload.decision_id,
            reasoning=payload.reasoning,
        )
    except StrategyAlreadyRegistered as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"strategy_id {payload.strategy_id} already registered",
        ) from exc
    return RegisterStrategyResponse(
        strategy_id=event.strategy_id,
        current_state=store.get_state(event.strategy_id),
        event=LifecycleEventResponse.from_event(event),
    )


@router.get(
    "/{strategy_id}/lifecycle",
    response_model=LifecycleHistoryResponse,
)
def get_lifecycle_history(
    strategy_id: str, request: Request
) -> LifecycleHistoryResponse:
    store = _store(request)
    try:
        history = store.get_history(strategy_id)
        current = store.get_state(strategy_id)
    except StrategyNotRegistered as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"strategy_id {strategy_id} not registered",
        ) from exc
    return LifecycleHistoryResponse(
        strategy_id=strategy_id,
        current_state=current,
        history=[LifecycleEventResponse.from_event(e) for e in history],
    )


@router.get(
    "",
    response_model=StrategyListResponse,
)
def list_strategies(request: Request) -> StrategyListResponse:
    store = _store(request)
    return StrategyListResponse(strategy_ids=list(store.list_strategies()))


# ---------------------------------------------------------------------------
# Internal: transition surface for in-process callers
# ---------------------------------------------------------------------------
#
# The CIO's arbitration loop (`cio.core.orchestrator`, `cio.core.arbiter`)
# calls into the lifecycle store directly — *not* through this HTTP router —
# to drive transitions and emit ActionType events. The HTTP surface above is
# intentionally read + register only. Wiring the in-process transition path
# is owned by the orchestrator/arbiter integration that follows this ticket
# (P1.3, sibling `petrosa-cio#115`).


def install_invalid_transition_handler(app: Any) -> None:  # pragma: no cover
    """Convert InvalidTransition into a 409 when the HTTP surface is extended."""

    from fastapi import FastAPI

    if not isinstance(app, FastAPI):
        return

    @app.exception_handler(InvalidTransition)
    async def _handle(_request: Request, exc: InvalidTransition):  # noqa: ANN001
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )


__all__ = [
    "LifecycleEventResponse",
    "LifecycleHistoryResponse",
    "RegisterStrategyRequest",
    "RegisterStrategyResponse",
    "StrategyListResponse",
    "install_invalid_transition_handler",
    "router",
]
