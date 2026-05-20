"""Strategy lifecycle authority + state machine (P1.2, #114).

Owns the live-state of every strategy the CIO governs. Transitions emit
`ActionType` events that the existing `OutputRouter` publishes on
`cio.lifecycle.<kind>.<strategy_id>`, so downstream consumers (data-manager
audit-trail, dashboard, FR9 history reader) see standing-state changes
without touching the per-intent signal path.

States and transitions:

    Registered (definition received)
        └── characterize() ──► Characterized (backtest complete)
                                 ├── admit()       ──► Admitted   [emits ADMIT]
                                 ├── admit_small() ──► OnTrial    [emits ADMIT_SMALL]
                                 └── reject()      ──► Rejected   [emits REJECT, terminal]
                          OnTrial
                                 ├── promote()     ──► Graduated  [emits PROMOTE]
                                 └── demote()      ──► Demoted    [emits DEMOTE]
                          Graduated
                                 ├── demote()      ──► Demoted    [emits DEMOTE]
                                 └── retire()      ──► Retired    [emits RETIRE, terminal]
                          Demoted
                                 ├── promote()     ──► Graduated  [emits PROMOTE]
                                 └── retire()      ──► Retired    [emits RETIRE, terminal]

Every transition carries a `decision_id` (P0.1 contract). The transition
helper raises `InvalidTransition` for any move not in the table above so
guards are explicit and testable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
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


class LifecycleState(StrEnum):
    """Strategy lifecycle states owned by the CIO."""

    REGISTERED = "registered"
    CHARACTERIZED = "characterized"
    ADMITTED = "admitted"
    ON_TRIAL = "on_trial"
    GRADUATED = "graduated"
    DEMOTED = "demoted"
    RETIRED = "retired"
    REJECTED = "rejected"


# Transitions allowed by the state machine. Key = (from_state, action),
# value = to_state. Any (from_state, action) tuple not present in this map
# results in `InvalidTransition`. The terminal states `RETIRED` and
# `REJECTED` have no outgoing transitions.
_ALLOWED: dict[tuple[LifecycleState, ActionType], LifecycleState] = {
    # Characterized branches
    (LifecycleState.CHARACTERIZED, ActionType.ADMIT): LifecycleState.ADMITTED,
    (LifecycleState.CHARACTERIZED, ActionType.ADMIT_SMALL): LifecycleState.ON_TRIAL,
    (LifecycleState.CHARACTERIZED, ActionType.REJECT): LifecycleState.REJECTED,
    # On-trial branches
    (LifecycleState.ON_TRIAL, ActionType.PROMOTE): LifecycleState.GRADUATED,
    (LifecycleState.ON_TRIAL, ActionType.DEMOTE): LifecycleState.DEMOTED,
    # Graduated branches
    (LifecycleState.GRADUATED, ActionType.DEMOTE): LifecycleState.DEMOTED,
    (LifecycleState.GRADUATED, ActionType.RETIRE): LifecycleState.RETIRED,
    # Demoted branches
    (LifecycleState.DEMOTED, ActionType.PROMOTE): LifecycleState.GRADUATED,
    (LifecycleState.DEMOTED, ActionType.RETIRE): LifecycleState.RETIRED,
    # Admitted branches (post-trial direct admission to full weight; may still retire)
    (LifecycleState.ADMITTED, ActionType.RETIRE): LifecycleState.RETIRED,
    (LifecycleState.ADMITTED, ActionType.DEMOTE): LifecycleState.DEMOTED,
}


class InvalidTransition(ValueError):
    """Raised when an unsupported (state, action) pair is attempted."""


class StrategyNotRegistered(KeyError):
    """Raised when an operation references an unknown strategy_id."""


class StrategyAlreadyRegistered(ValueError):
    """Raised when `register()` is called twice for the same strategy_id."""


@dataclass(frozen=True)
class LifecycleEvent:
    """A single transition recorded in the lifecycle history.

    Each event carries the cross-service `decision_id` so audit-trail consumers
    can join lifecycle events with intent / decision / execution records.
    """

    strategy_id: str
    from_state: LifecycleState | None
    to_state: LifecycleState
    action: ActionType | None
    decision_id: str
    reasoning: dict[str, Any]
    at: datetime


@dataclass
class StrategyLifecycle:
    """In-memory lifecycle record for a single strategy."""

    strategy_id: str
    definition: dict[str, Any]
    current_state: LifecycleState
    history: list[LifecycleEvent] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(UTC)


def _new_decision_id() -> str:
    return uuid.uuid4().hex


class StrategyLifecycleStore:
    """In-memory authority for strategy lifecycles.

    Thread-safe via a single mutex (lifecycle transitions are low-throughput).
    Persistence is intentionally pluggable: the public API exposes only what
    a future Mongo- or Qdrant-backed store would need to satisfy. The audit-
    trail itself (the durable record of state changes) is owned by
    `petrosa-data-manager` via the `cio.lifecycle.<kind>.<sid>` NATS subjects.

    Per FR9: `get_history(strategy_id)` returns the full sequence of
    transitions for an operator view; per the P0.1 contract, every event
    in the history carries a `decision_id`.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._strategies: dict[str, StrategyLifecycle] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        strategy_id: str,
        definition: dict[str, Any],
        *,
        decision_id: str | None = None,
        reasoning: dict[str, Any] | None = None,
    ) -> LifecycleEvent:
        """Register a new strategy in `REGISTERED` state.

        Raises `StrategyAlreadyRegistered` if the strategy_id already exists.
        Returns the genesis `LifecycleEvent` (no `from_state`, no `action`).
        """
        decision_id = decision_id or _new_decision_id()
        with self._lock:
            if strategy_id in self._strategies:
                raise StrategyAlreadyRegistered(strategy_id)
            event = LifecycleEvent(
                strategy_id=strategy_id,
                from_state=None,
                to_state=LifecycleState.REGISTERED,
                action=None,
                decision_id=decision_id,
                reasoning=dict(reasoning or {}),
                at=_now(),
            )
            self._strategies[strategy_id] = StrategyLifecycle(
                strategy_id=strategy_id,
                definition=dict(definition),
                current_state=LifecycleState.REGISTERED,
                history=[event],
            )
        return event

    # ------------------------------------------------------------------
    # Internal transition primitive
    # ------------------------------------------------------------------

    def _transition(
        self,
        strategy_id: str,
        action: ActionType,
        *,
        decision_id: str | None,
        reasoning: dict[str, Any] | None,
    ) -> LifecycleEvent:
        decision_id = decision_id or _new_decision_id()
        with self._lock:
            record = self._strategies.get(strategy_id)
            if record is None:
                raise StrategyNotRegistered(strategy_id)
            from_state = record.current_state
            try:
                to_state = _ALLOWED[(from_state, action)]
            except KeyError as exc:
                raise InvalidTransition(
                    f"strategy={strategy_id} cannot {action.value} "
                    f"from state {from_state.value}"
                ) from exc
            event = LifecycleEvent(
                strategy_id=strategy_id,
                from_state=from_state,
                to_state=to_state,
                action=action,
                decision_id=decision_id,
                reasoning=dict(reasoning or {}),
                at=_now(),
            )
            record.current_state = to_state
            record.history.append(event)
        return event

    # ------------------------------------------------------------------
    # Public transition helpers
    # ------------------------------------------------------------------

    def characterize(
        self,
        strategy_id: str,
        *,
        decision_id: str | None = None,
        reasoning: dict[str, Any] | None = None,
    ) -> LifecycleEvent:
        """Move REGISTERED → CHARACTERIZED. Internal transition, no ActionType emitted.

        Characterization is a backtest-complete signal; downstream consumers
        do not need a NATS event for it, so this transition does not produce
        an `ActionType`. Caller passes the resulting `LifecycleEvent` to the
        history; nothing is published.
        """
        decision_id = decision_id or _new_decision_id()
        with self._lock:
            record = self._strategies.get(strategy_id)
            if record is None:
                raise StrategyNotRegistered(strategy_id)
            if record.current_state is not LifecycleState.REGISTERED:
                raise InvalidTransition(
                    f"strategy={strategy_id} cannot characterize from state "
                    f"{record.current_state.value}"
                )
            event = LifecycleEvent(
                strategy_id=strategy_id,
                from_state=LifecycleState.REGISTERED,
                to_state=LifecycleState.CHARACTERIZED,
                action=None,
                decision_id=decision_id,
                reasoning=dict(reasoning or {}),
                at=_now(),
            )
            record.current_state = LifecycleState.CHARACTERIZED
            record.history.append(event)
        return event

    def admit(
        self,
        strategy_id: str,
        *,
        decision_id: str | None = None,
        reasoning: dict[str, Any] | None = None,
    ) -> LifecycleEvent:
        return self._transition(
            strategy_id,
            ActionType.ADMIT,
            decision_id=decision_id,
            reasoning=reasoning,
        )

    def admit_small(
        self,
        strategy_id: str,
        *,
        decision_id: str | None = None,
        reasoning: dict[str, Any] | None = None,
    ) -> LifecycleEvent:
        return self._transition(
            strategy_id,
            ActionType.ADMIT_SMALL,
            decision_id=decision_id,
            reasoning=reasoning,
        )

    def reject(
        self,
        strategy_id: str,
        *,
        decision_id: str | None = None,
        reasoning: dict[str, Any] | None = None,
    ) -> LifecycleEvent:
        return self._transition(
            strategy_id,
            ActionType.REJECT,
            decision_id=decision_id,
            reasoning=reasoning,
        )

    def promote(
        self,
        strategy_id: str,
        *,
        decision_id: str | None = None,
        reasoning: dict[str, Any] | None = None,
    ) -> LifecycleEvent:
        return self._transition(
            strategy_id,
            ActionType.PROMOTE,
            decision_id=decision_id,
            reasoning=reasoning,
        )

    def demote(
        self,
        strategy_id: str,
        *,
        decision_id: str | None = None,
        reasoning: dict[str, Any] | None = None,
    ) -> LifecycleEvent:
        return self._transition(
            strategy_id,
            ActionType.DEMOTE,
            decision_id=decision_id,
            reasoning=reasoning,
        )

    def retire(
        self,
        strategy_id: str,
        *,
        decision_id: str | None = None,
        reasoning: dict[str, Any] | None = None,
    ) -> LifecycleEvent:
        return self._transition(
            strategy_id,
            ActionType.RETIRE,
            decision_id=decision_id,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Read API (feeds FR9)
    # ------------------------------------------------------------------

    def get_state(self, strategy_id: str) -> LifecycleState:
        with self._lock:
            record = self._strategies.get(strategy_id)
            if record is None:
                raise StrategyNotRegistered(strategy_id)
            return record.current_state

    def get_history(self, strategy_id: str) -> list[LifecycleEvent]:
        with self._lock:
            record = self._strategies.get(strategy_id)
            if record is None:
                raise StrategyNotRegistered(strategy_id)
            return list(record.history)

    def get_definition(self, strategy_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._strategies.get(strategy_id)
            if record is None:
                raise StrategyNotRegistered(strategy_id)
            return dict(record.definition)

    def list_strategies(self) -> Iterable[str]:
        with self._lock:
            return list(self._strategies.keys())


__all__ = [
    "InvalidTransition",
    "LifecycleEvent",
    "LifecycleState",
    "StrategyAlreadyRegistered",
    "StrategyLifecycle",
    "StrategyLifecycleStore",
    "StrategyNotRegistered",
]
