"""Per-ActionType authority configuration + pending-approval queue (P1.3, #115).

Runtime-mutable governance layer that sits in front of `OutputRouter.route()`.
Each `ActionType` has one of three authority states:

  ENABLED                       — current behavior; the action is dispatched as-is
  OPERATOR_APPROVAL_REQUIRED    — the decision is diverted to a pending queue
                                   surfaced to the operator dashboard; nothing
                                   is dispatched until `approve_pending()` fires
  DISABLED                      — the action is replaced with a next-best safe
                                   fallback; the substituted action proceeds
                                   through the normal dispatch path

Defaults are all `ENABLED` so existing behavior is preserved until an operator
mutates the store. Every authority change is appended to an audit log with the
operator identity supplied by the caller (single-operator deployment today;
the field is recorded so the audit-trail is intact when SSO lands).

This module is intentionally self-contained — no NATS, no HTTP. The HTTP
surface lives in `cio.apps.authority_api`; the router consults this store via
`cio.core.router`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover - py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - py310 fallback
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]  # noqa: UP042
        pass


from cio.models.enums import ActionType


class ActionAuthority(StrEnum):
    """Per-ActionType authority state."""

    ENABLED = "enabled"
    OPERATOR_APPROVAL_REQUIRED = "operator_approval_required"
    DISABLED = "disabled"


# Next-best safe fallback when an action is DISABLED. The fallback action is
# dispatched in place of the original; it is NOT itself subject to a recursive
# authority check (to keep the substitution deterministic and avoid loops).
#
# Design rule: the fallback must be a strictly *safer* action — never expand
# scope, never start a trade that the original would not have started.
DEFAULT_FALLBACKS: dict[ActionType, ActionType] = {
    ActionType.EXECUTE: ActionType.SKIP,
    ActionType.MODIFY_PARAMS: ActionType.SKIP,
    ActionType.PAUSE_STRATEGY: ActionType.BLOCK,
    ActionType.ESCALATE: ActionType.BLOCK,
    ActionType.RETRY_SAFE: ActionType.SKIP,
    ActionType.FAIL_SAFE: ActionType.BLOCK,
    ActionType.DOWN_WEIGHT: ActionType.SKIP,
    ActionType.THROTTLE: ActionType.SKIP,
    ActionType.VETO: ActionType.BLOCK,
    # Lifecycle actions (P1.2): admit/admit_small/promote downgrade to reject;
    # demote/retire downgrade to skip (no-op rather than action).
    ActionType.ADMIT: ActionType.REJECT,
    ActionType.ADMIT_SMALL: ActionType.REJECT,
    ActionType.PROMOTE: ActionType.SKIP,
    ActionType.DEMOTE: ActionType.SKIP,
    ActionType.RETIRE: ActionType.SKIP,
    # SKIP, BLOCK, REJECT are already terminal-safe; they cannot be "disabled"
    # meaningfully. The store still records the state for completeness and
    # `apply_authority` simply returns SKIP for any disabled action whose
    # mapping is missing here (see `_resolve_fallback`).
}


class _Sentinel:
    """Marker for pending-queue diversion vs disabled-fallback substitution."""


@dataclass(frozen=True)
class AuthorityChange:
    """An audit-log entry: who changed what, when, and why."""

    action: ActionType
    from_state: ActionAuthority
    to_state: ActionAuthority
    operator_id: str
    reason: str
    at: datetime


@dataclass(frozen=True)
class PendingDecision:
    """A decision held in the operator-approval queue.

    The decision and trigger context are stored verbatim (`dict` form) so the
    operator's approve/reject path can reconstruct the dispatch without
    re-running the reasoning loop.
    """

    queue_id: str
    action: ActionType
    strategy_id: str
    decision_id: str
    correlation_id: str
    context_payload: dict[str, Any]
    decision_payload: dict[str, Any]
    at: datetime


@dataclass(frozen=True)
class PendingResolution:
    """Outcome of approve_pending / reject_pending."""

    queue_id: str
    approved: bool
    operator_id: str
    reason: str
    at: datetime
    pending: PendingDecision


class AuthorityStore:
    """Thread-safe in-memory authority configuration + pending-decision queue.

    Public surface mirrors the FastAPI router's needs:
        - `get_state(action) -> ActionAuthority`
        - `set_state(action, new_state, *, operator_id, reason) -> AuthorityChange`
        - `get_all() -> dict[ActionType, ActionAuthority]`
        - `get_audit() -> list[AuthorityChange]`
        - `enqueue_pending(...)` / `list_pending()`
        - `approve_pending(queue_id, *, operator_id, reason)`
        - `reject_pending(queue_id, *, operator_id, reason)`

    Persistence is intentionally pluggable — a future Mongo- or Qdrant-backed
    implementation can drop in behind this API without changing callers.
    """

    def __init__(
        self,
        defaults: dict[ActionType, ActionAuthority] | None = None,
        fallbacks: dict[ActionType, ActionType] | None = None,
    ) -> None:
        self._lock = Lock()
        self._state: dict[ActionType, ActionAuthority] = {
            a: ActionAuthority.ENABLED for a in ActionType
        }
        if defaults:
            self._state.update(defaults)
        self._audit: list[AuthorityChange] = []
        self._pending: dict[str, PendingDecision] = {}
        self._fallbacks: dict[ActionType, ActionType] = dict(
            fallbacks if fallbacks is not None else DEFAULT_FALLBACKS
        )

    # ------------------------------------------------------------------
    # Authority CRUD
    # ------------------------------------------------------------------

    def get_state(self, action: ActionType) -> ActionAuthority:
        with self._lock:
            return self._state[action]

    def get_all(self) -> dict[ActionType, ActionAuthority]:
        with self._lock:
            return dict(self._state)

    def set_state(
        self,
        action: ActionType,
        new_state: ActionAuthority,
        *,
        operator_id: str,
        reason: str,
    ) -> AuthorityChange:
        if not operator_id:
            raise ValueError("operator_id is required for authority change")
        with self._lock:
            previous = self._state[action]
            self._state[action] = new_state
            change = AuthorityChange(
                action=action,
                from_state=previous,
                to_state=new_state,
                operator_id=operator_id,
                reason=reason,
                at=datetime.now(UTC),
            )
            self._audit.append(change)
        return change

    def get_audit(self) -> list[AuthorityChange]:
        with self._lock:
            return list(self._audit)

    # ------------------------------------------------------------------
    # Fallback resolution (DISABLED)
    # ------------------------------------------------------------------

    def get_fallback(self, action: ActionType) -> ActionType:
        """Resolve the next-best safe action for a DISABLED `action`.

        Missing entries default to `SKIP` (the safest no-op). `SKIP`, `BLOCK`,
        and `REJECT` are themselves terminal-safe — disabling them is a
        configuration mistake the caller should treat as a no-op rather than
        an error.
        """
        with self._lock:
            return self._fallbacks.get(action, ActionType.SKIP)

    # ------------------------------------------------------------------
    # Pending-approval queue
    # ------------------------------------------------------------------

    def enqueue_pending(
        self,
        *,
        action: ActionType,
        strategy_id: str,
        decision_id: str,
        correlation_id: str,
        context_payload: dict[str, Any],
        decision_payload: dict[str, Any],
    ) -> PendingDecision:
        queue_id = uuid.uuid4().hex
        pending = PendingDecision(
            queue_id=queue_id,
            action=action,
            strategy_id=strategy_id,
            decision_id=decision_id,
            correlation_id=correlation_id,
            context_payload=dict(context_payload),
            decision_payload=dict(decision_payload),
            at=datetime.now(UTC),
        )
        with self._lock:
            self._pending[queue_id] = pending
        return pending

    def list_pending(self) -> list[PendingDecision]:
        with self._lock:
            return list(self._pending.values())

    def get_pending(self, queue_id: str) -> PendingDecision:
        with self._lock:
            if queue_id not in self._pending:
                raise KeyError(queue_id)
            return self._pending[queue_id]

    def approve_pending(
        self,
        queue_id: str,
        *,
        operator_id: str,
        reason: str = "",
    ) -> PendingResolution:
        return self._resolve_pending(
            queue_id, approved=True, operator_id=operator_id, reason=reason
        )

    def reject_pending(
        self,
        queue_id: str,
        *,
        operator_id: str,
        reason: str = "",
    ) -> PendingResolution:
        return self._resolve_pending(
            queue_id, approved=False, operator_id=operator_id, reason=reason
        )

    def _resolve_pending(
        self,
        queue_id: str,
        *,
        approved: bool,
        operator_id: str,
        reason: str,
    ) -> PendingResolution:
        if not operator_id:
            raise ValueError("operator_id is required for pending resolution")
        with self._lock:
            pending = self._pending.pop(queue_id, None)
            if pending is None:
                raise KeyError(queue_id)
        return PendingResolution(
            queue_id=queue_id,
            approved=approved,
            operator_id=operator_id,
            reason=reason,
            at=datetime.now(UTC),
            pending=pending,
        )


# ---------------------------------------------------------------------------
# Decision-time helpers (used by OutputRouter)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorityDecision:
    """Resolution of an authority check at dispatch time.

    One of three outcomes:
        - dispatch the original action (`action == original`)
        - dispatch a fallback action (`action != original`, `was_disabled=True`)
        - hold for operator approval (`pending` is not None, no dispatch)
    """

    action: ActionType
    pending: PendingDecision | None
    was_disabled: bool
    original: ActionType


def apply_authority(
    store: AuthorityStore,
    *,
    action: ActionType,
    strategy_id: str,
    decision_id: str,
    correlation_id: str,
    context_payload: dict[str, Any],
    decision_payload: dict[str, Any],
) -> AuthorityDecision:
    """Apply the authority store's per-action policy at dispatch time.

    Caller (`OutputRouter`) consults the returned `AuthorityDecision`:
        - `pending is not None` → divert to the pending queue, return early
        - `was_disabled` → dispatch `action` (the fallback) instead of `original`
        - otherwise → dispatch `action` as normal

    Authority lookup, fallback resolution, and pending enqueue are all
    serialized by the store's internal lock, so concurrent dispatch is safe.
    """
    state = store.get_state(action)
    if state is ActionAuthority.ENABLED:
        return AuthorityDecision(
            action=action, pending=None, was_disabled=False, original=action
        )
    if state is ActionAuthority.OPERATOR_APPROVAL_REQUIRED:
        pending = store.enqueue_pending(
            action=action,
            strategy_id=strategy_id,
            decision_id=decision_id,
            correlation_id=correlation_id,
            context_payload=context_payload,
            decision_payload=decision_payload,
        )
        return AuthorityDecision(
            action=action, pending=pending, was_disabled=False, original=action
        )
    # DISABLED
    fallback = store.get_fallback(action)
    return AuthorityDecision(
        action=fallback, pending=None, was_disabled=True, original=action
    )


__all__ = [
    "DEFAULT_FALLBACKS",
    "ActionAuthority",
    "AuthorityChange",
    "AuthorityDecision",
    "AuthorityStore",
    "PendingDecision",
    "PendingResolution",
    "apply_authority",
]
