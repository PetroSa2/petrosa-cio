"""FastAPI router for per-action authority + pending-approval queue (P1.3, #115).

Exposes:
  GET    /authority                       — current state of every ActionType
  GET    /authority/{action}              — state of a single action
  PUT    /authority/{action}              — operator mutates the state
  GET    /authority/audit                 — change history (operator id + reason)
  GET    /authority/pending               — list decisions awaiting approval
  POST   /authority/pending/{queue_id}/approve  — operator approves; payload dispatches
  POST   /authority/pending/{queue_id}/reject   — operator rejects; payload discarded

The router is mounted by `cio.main`. It reads/writes through an
`AuthorityStore` instance attached to `app.state.authority_store`.

Operator identity (`operator_id`) is passed in the request body for mutating
calls and recorded on every audit-log entry. Single-operator deployment
today; the field is captured so the audit trail is intact when SSO lands.

`POST /authority/pending/{queue_id}/approve` does NOT re-dispatch the
decision from this HTTP path — the approval result is returned to the
caller, which is expected to feed the payload back into `OutputRouter` (or
the calling test). The router is intentionally pure REST over the store;
dispatching from a request handler would introduce a circular import.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from cio.core.authority import (
    ActionAuthority,
    AuthorityChange,
    AuthorityStore,
    PendingDecision,
    PendingResolution,
)
from cio.models.enums import ActionType

router = APIRouter(prefix="/authority", tags=["authority"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AuthorityStateResponse(BaseModel):
    action: ActionType
    state: ActionAuthority


class AuthorityListResponse(BaseModel):
    states: list[AuthorityStateResponse]


class AuthorityUpdateRequest(BaseModel):
    state: ActionAuthority
    operator_id: str = Field(..., min_length=1)
    reason: str = Field(default="")


class AuthorityChangeResponse(BaseModel):
    action: ActionType
    from_state: ActionAuthority
    to_state: ActionAuthority
    operator_id: str
    reason: str
    at: datetime

    @classmethod
    def from_change(cls, change: AuthorityChange) -> AuthorityChangeResponse:
        return cls(
            action=change.action,
            from_state=change.from_state,
            to_state=change.to_state,
            operator_id=change.operator_id,
            reason=change.reason,
            at=change.at,
        )


class AuthorityAuditResponse(BaseModel):
    changes: list[AuthorityChangeResponse]


class PendingDecisionResponse(BaseModel):
    queue_id: str
    action: ActionType
    strategy_id: str
    decision_id: str
    correlation_id: str
    context_payload: dict[str, Any]
    decision_payload: dict[str, Any]
    at: datetime

    @classmethod
    def from_pending(cls, pending: PendingDecision) -> PendingDecisionResponse:
        return cls(
            queue_id=pending.queue_id,
            action=pending.action,
            strategy_id=pending.strategy_id,
            decision_id=pending.decision_id,
            correlation_id=pending.correlation_id,
            context_payload=pending.context_payload,
            decision_payload=pending.decision_payload,
            at=pending.at,
        )


class PendingListResponse(BaseModel):
    pending: list[PendingDecisionResponse]


class ResolvePendingRequest(BaseModel):
    operator_id: str = Field(..., min_length=1)
    reason: str = Field(default="")


class ResolvePendingResponse(BaseModel):
    queue_id: str
    approved: bool
    operator_id: str
    reason: str
    at: datetime
    pending: PendingDecisionResponse

    @classmethod
    def from_resolution(cls, resolution: PendingResolution) -> ResolvePendingResponse:
        return cls(
            queue_id=resolution.queue_id,
            approved=resolution.approved,
            operator_id=resolution.operator_id,
            reason=resolution.reason,
            at=resolution.at,
            pending=PendingDecisionResponse.from_pending(resolution.pending),
        )


# ---------------------------------------------------------------------------
# Store access
# ---------------------------------------------------------------------------


def _store(request: Request) -> AuthorityStore:
    store = getattr(request.app.state, "authority_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authority store not initialized",
        )
    return store


# ---------------------------------------------------------------------------
# Authority CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=AuthorityListResponse)
def list_authority(request: Request) -> AuthorityListResponse:
    store = _store(request)
    states = store.get_all()
    return AuthorityListResponse(
        states=[
            AuthorityStateResponse(action=action, state=state)
            for action, state in sorted(states.items(), key=lambda kv: kv[0].value)
        ]
    )


@router.get("/audit", response_model=AuthorityAuditResponse)
def get_authority_audit(request: Request) -> AuthorityAuditResponse:
    store = _store(request)
    return AuthorityAuditResponse(
        changes=[AuthorityChangeResponse.from_change(c) for c in store.get_audit()]
    )


@router.get("/pending", response_model=PendingListResponse)
def list_pending(request: Request) -> PendingListResponse:
    store = _store(request)
    return PendingListResponse(
        pending=[PendingDecisionResponse.from_pending(p) for p in store.list_pending()]
    )


@router.post(
    "/pending/{queue_id}/approve",
    response_model=ResolvePendingResponse,
)
def approve_pending(
    queue_id: str, payload: ResolvePendingRequest, request: Request
) -> ResolvePendingResponse:
    store = _store(request)
    try:
        resolution = store.approve_pending(
            queue_id,
            operator_id=payload.operator_id,
            reason=payload.reason,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"pending queue_id {queue_id} not found",
        ) from exc
    return ResolvePendingResponse.from_resolution(resolution)


@router.post(
    "/pending/{queue_id}/reject",
    response_model=ResolvePendingResponse,
)
def reject_pending(
    queue_id: str, payload: ResolvePendingRequest, request: Request
) -> ResolvePendingResponse:
    store = _store(request)
    try:
        resolution = store.reject_pending(
            queue_id,
            operator_id=payload.operator_id,
            reason=payload.reason,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"pending queue_id {queue_id} not found",
        ) from exc
    return ResolvePendingResponse.from_resolution(resolution)


@router.get("/{action}", response_model=AuthorityStateResponse)
def get_authority(action: ActionType, request: Request) -> AuthorityStateResponse:
    store = _store(request)
    return AuthorityStateResponse(action=action, state=store.get_state(action))


@router.put("/{action}", response_model=AuthorityChangeResponse)
def set_authority(
    action: ActionType, payload: AuthorityUpdateRequest, request: Request
) -> AuthorityChangeResponse:
    store = _store(request)
    change = store.set_state(
        action,
        payload.state,
        operator_id=payload.operator_id,
        reason=payload.reason,
    )
    return AuthorityChangeResponse.from_change(change)


__all__ = [
    "AuthorityAuditResponse",
    "AuthorityChangeResponse",
    "AuthorityListResponse",
    "AuthorityStateResponse",
    "AuthorityUpdateRequest",
    "PendingDecisionResponse",
    "PendingListResponse",
    "ResolvePendingRequest",
    "ResolvePendingResponse",
    "router",
]
