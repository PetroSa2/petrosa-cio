"""CIO health evaluator (P7.1, #610).

Emits ``evaluator.cio.verdict`` so Outcome 5's "all 8 services reporting"
gate closes. Verdict is driven by structural + light behavioral checks
against recent CIO activity — per-action-type outcome grading is
explicitly deferred to Phase 2.

The MVP verdict combines three signals over a sliding window:

1. **Reasoning-context presence (structural).** Recent CIO decisions
   must carry a non-empty, non-fallback ``thought_trace``. Empty traces
   or known fallback markers (``PARSE_FAILURE``, ``SYSTEM_ERROR``,
   ``CRITICAL_FAILURE_ENFORCEMENT``, ``TIMEOUT_ENFORCEMENT``,
   ``DETERMINISTIC_BYPASS``) indicate the LLM-degraded path. If the
   fraction of fallback-marked decisions in the window exceeds
   ``missing_context_threshold`` the evaluator reports unhealthy.

2. **Degraded-mode dominance (behavioral).** Sustained dominance of
   ``FAIL_SAFE`` / ``SKIP`` actions over the window indicates the safe-
   fail path is on (FR13 / NFR-R5). If that fraction exceeds
   ``degraded_threshold`` the evaluator reports unhealthy.

3. **Cadence / silence (behavioral).** When ``cio.intent.trading.>`` is
   active in the window but ``signals.trading.>`` is silent, CIO is
   receiving intents but not emitting any signals (or audit-copy
   decisions). A long silence flag is unhealthy.

Per-decision realized-outcome correlation is persisted via the optional
:class:`OutcomeCorrelator` (Phase-2 substrate) but does NOT feed the
verdict in MVP. The hook is in place so the Phase-2 judgment grader can
build on the same audit copy without a second observability rewrite.

Hysteresis: the evaluator emits at every tick but only flips its
externally-published verdict after observing the same raw verdict for
``stable_ticks_required`` consecutive ticks. This avoids flapping when
the window straddles a single anomalous decision.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

if TYPE_CHECKING:
    from nats.aio.client import Client as NATS

logger = logging.getLogger(__name__)


VERDICT_SUBJECT = "evaluator.cio.verdict"
DECISION_AUDIT_PATTERN = "cio.decision.audit.>"
INTENT_PATTERN = "cio.intent.trading.>"
SIGNAL_PATTERN = "signals.trading.>"

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"

# Action values that count as "degraded mode" — these come straight from
# `cio.models.enums.ActionType` but the evaluator keeps them as raw
# strings so it can tolerate enum drift across releases without hard
# import coupling on the audit subject's payload.
DEGRADED_ACTIONS: frozenset[str] = frozenset({"fail_safe", "skip"})

# Thought-trace markers we treat as "missing reasoning context". These
# match the SAFE_DEFAULTS / fallback markers produced by
# `cio.models.decision` and `cio.core.orchestrator` when the LLM path
# degrades or is bypassed.
FALLBACK_TRACE_MARKERS: frozenset[str] = frozenset(
    {
        "PARSE_FAILURE",
        "SYSTEM_ERROR",
        "CRITICAL_FAILURE_ENFORCEMENT",
        "TIMEOUT_ENFORCEMENT",
        "DETERMINISTIC_BYPASS",
    }
)


@dataclass(slots=True)
class _DecisionRecord:
    observed_at: datetime
    action: str
    thought_trace: str
    decision_id: str
    strategy_id: str
    correlation_id: str


@dataclass(slots=True)
class _EventTick:
    observed_at: datetime


class OutcomeCorrelator:
    """Persists decision/outcome correlation records.

    MVP scope: hook in place. When a P&L event lands matching a recently
    observed decision_id, the correlator writes a single audit record
    via the same vector_client used by the OutputRouter. This is the
    "Phase-2-ready substrate" called out in the ticket AC.

    In MVP no NATS subscription is created here — the wiring point is
    intentionally narrow so Phase 2 can attach its own subject
    convention without churning this class.
    """

    def __init__(self, vector_client) -> None:
        self._vc = vector_client

    async def record(
        self,
        *,
        decision_id: str,
        strategy_id: str,
        correlation_id: str,
        outcome_payload: dict,
    ) -> None:
        """Persist one correlation record. Failures are logged, not raised."""
        try:
            await self._vc.upsert(
                strategy_id=strategy_id,
                payload={
                    "event_type": "decision_outcome_correlation",
                    "decision_id": decision_id,
                    "correlation_id": correlation_id,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "outcome": outcome_payload,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "outcome_correlation_persist_failed",
                extra={
                    "decision_id": decision_id,
                    "strategy_id": strategy_id,
                    "error": str(exc),
                },
            )


class CIOHealthEvaluator:
    """Emits ``evaluator.cio.verdict`` from sliding-window CIO activity.

    The class is split into two cooperating loops:

    * **Observation loop** — three NATS subscriptions feed the
      sliding-window deques (decisions, intents, signal-outputs).
    * **Emit loop** — a periodic task computes the raw verdict, applies
      hysteresis, and publishes on ``evaluator.cio.verdict``.

    Both loops are non-blocking: NATS handlers append to deques and
    return; the emit loop reads them at a coarse cadence. State is
    per-process / in-memory (consistent with the existing
    EvaluatorSubscriber's posture).
    """

    def __init__(
        self,
        nats_client: NATS,
        *,
        window: timedelta = timedelta(seconds=60),
        emit_interval: timedelta = timedelta(seconds=15),
        stable_ticks_required: int = 2,
        missing_context_threshold: float = 0.5,
        degraded_threshold: float = 0.5,
        min_intents_for_silence_check: int = 3,
        silence_min_age: timedelta = timedelta(seconds=20),
        on_emit: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._nc = nats_client
        self._window = window
        self._emit_interval = emit_interval
        self._stable_ticks_required = max(1, stable_ticks_required)
        self._missing_context_threshold = missing_context_threshold
        self._degraded_threshold = degraded_threshold
        self._min_intents_for_silence_check = min_intents_for_silence_check
        self._silence_min_age = silence_min_age
        self._on_emit = on_emit

        self._decisions: deque[_DecisionRecord] = deque()
        self._intents: deque[_EventTick] = deque()
        self._signals: deque[_EventTick] = deque()

        # NATS Subscription is loosely typed (the upstream `nats-py`
        # client uses a runtime-private class) — keep these as Any-typed
        # ``object`` slots so mypy doesn't infer ``None`` and reject
        # later assignments.
        self._decision_sub: object | None = None
        self._intent_sub: object | None = None
        self._signal_sub: object | None = None
        self._emit_task: asyncio.Task | None = None

        # Hysteresis state. ``_emitted_verdict`` is the last verdict we
        # actually published; ``_candidate_verdict`` is the raw verdict
        # we've been observing consecutively, with ``_candidate_streak``
        # counting consecutive observations.
        self._emitted_verdict: str = UNKNOWN
        self._candidate_verdict: str = UNKNOWN
        self._candidate_streak: int = 0

    # ----- lifecycle -----

    async def start(self) -> None:
        self._decision_sub = await self._nc.subscribe(
            DECISION_AUDIT_PATTERN, cb=self._on_decision
        )
        self._intent_sub = await self._nc.subscribe(INTENT_PATTERN, cb=self._on_intent)
        self._signal_sub = await self._nc.subscribe(SIGNAL_PATTERN, cb=self._on_signal)
        self._emit_task = asyncio.create_task(self._emit_loop())
        logger.info(
            "cio_health_evaluator_started",
            extra={
                "decision_subject": DECISION_AUDIT_PATTERN,
                "intent_subject": INTENT_PATTERN,
                "signal_subject": SIGNAL_PATTERN,
                "verdict_subject": VERDICT_SUBJECT,
                "window_s": self._window.total_seconds(),
                "emit_interval_s": self._emit_interval.total_seconds(),
            },
        )

    async def stop(self) -> None:
        if self._emit_task is not None:
            self._emit_task.cancel()
            try:
                await self._emit_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._emit_task = None
        for sub in (self._decision_sub, self._intent_sub, self._signal_sub):
            if sub is None:
                continue
            unsubscribe = getattr(sub, "unsubscribe", None)
            if unsubscribe is None:
                continue
            try:
                await unsubscribe()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"cio_health_evaluator unsubscribe failed: {exc}")
        self._decision_sub = self._intent_sub = self._signal_sub = None

    # ----- NATS handlers -----

    async def _on_decision(self, msg) -> None:
        try:
            payload = json.loads(msg.data.decode())
        except (json.JSONDecodeError, AttributeError, UnicodeDecodeError) as exc:
            logger.warning(
                "health_evaluator_decision_unparsable",
                extra={"subject": msg.subject, "error": str(exc)},
            )
            return

        record = _DecisionRecord(
            observed_at=datetime.now(UTC),
            action=str(payload.get("action") or "").lower(),
            thought_trace=(payload.get("thought_trace") or "").strip(),
            decision_id=str(payload.get("decision_id") or ""),
            strategy_id=str(payload.get("strategy_id") or ""),
            correlation_id=str(payload.get("correlation_id") or ""),
        )
        self._decisions.append(record)

    async def _on_intent(self, msg) -> None:
        self._intents.append(_EventTick(observed_at=datetime.now(UTC)))

    async def _on_signal(self, msg) -> None:
        self._signals.append(_EventTick(observed_at=datetime.now(UTC)))

    # ----- evaluation -----

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._window
        for q in (self._decisions, self._intents, self._signals):
            while q and q[0].observed_at < cutoff:
                q.popleft()

    def evaluate(self, now: datetime | None = None) -> tuple[str, str]:
        """Compute the raw verdict + reason for the current window.

        Exposed for testability — the emit loop calls this with
        ``now = datetime.now(UTC)`` on each tick.

        Returns ``(verdict, reason)`` where verdict is one of HEALTHY /
        UNHEALTHY / UNKNOWN and reason is the verbatim-render text the
        dashboard surfaces (NFR-O5).
        """
        now = now or datetime.now(UTC)
        self._prune(now)

        decisions = list(self._decisions)
        intents = list(self._intents)
        signals = list(self._signals)

        # Silence check first — covers the case where intents are
        # flowing but CIO has produced nothing (neither decisions nor
        # signal outputs). Require the oldest qualifying intent to be at
        # least ``silence_min_age`` old, otherwise a fresh burst can
        # trip the check prematurely on startup.
        if (
            len(intents) >= self._min_intents_for_silence_check
            and not signals
            and not decisions
        ):
            oldest = intents[0].observed_at
            if (now - oldest) >= self._silence_min_age:
                return (
                    UNHEALTHY,
                    (
                        f"silence: {len(intents)} intents on "
                        "cio.intent.trading.> in last "
                        f"{int(self._window.total_seconds())}s, no "
                        "signals.trading.> emitted"
                    ),
                )

        if not decisions:
            return UNKNOWN, "no recent CIO decisions observed in window"

        total = len(decisions)
        missing_context = sum(
            1
            for d in decisions
            if not d.thought_trace or d.thought_trace in FALLBACK_TRACE_MARKERS
        )
        degraded = sum(1 for d in decisions if d.action in DEGRADED_ACTIONS)

        missing_fraction = missing_context / total
        degraded_fraction = degraded / total

        if missing_fraction > self._missing_context_threshold:
            pct = round(missing_fraction * 100)
            return (
                UNHEALTHY,
                (
                    f"reasoning-context missing on {pct}% of recent decisions "
                    f"({missing_context}/{total}) — fallback path active"
                ),
            )

        if degraded_fraction > self._degraded_threshold:
            pct = round(degraded_fraction * 100)
            return (
                UNHEALTHY,
                (
                    f"degraded-mode dominance: {pct}% of recent decisions "
                    f"({degraded}/{total}) in FAIL_SAFE/SKIP"
                ),
            )

        return (
            HEALTHY,
            (
                f"{total} decisions in last "
                f"{int(self._window.total_seconds())}s, "
                f"missing-context {missing_context}, degraded {degraded}"
            ),
        )

    def _apply_hysteresis(self, raw_verdict: str) -> str:
        """Return the verdict the evaluator should publish this tick.

        Same raw verdict for ``stable_ticks_required`` consecutive ticks
        flips the externally-published verdict. Until then, the previous
        emitted verdict stays in place (avoids flapping).
        """
        if raw_verdict == self._emitted_verdict:
            self._candidate_verdict = raw_verdict
            self._candidate_streak = 0
            return self._emitted_verdict

        if raw_verdict == self._candidate_verdict:
            self._candidate_streak += 1
        else:
            self._candidate_verdict = raw_verdict
            self._candidate_streak = 1

        if self._candidate_streak >= self._stable_ticks_required:
            self._emitted_verdict = raw_verdict
            self._candidate_streak = 0
        return self._emitted_verdict

    async def _publish(self, verdict: str, reason: str) -> None:
        payload = json.dumps({"verdict": verdict, "reason": reason[:200]}).encode()
        try:
            await self._nc.publish(VERDICT_SUBJECT, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cio_health_evaluator_publish_failed",
                extra={"error": str(exc), "verdict": verdict},
            )
            return
        if self._on_emit is not None:
            try:
                await self._on_emit(verdict, reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"cio_health_evaluator on_emit failed: {exc}")

    async def tick(self) -> tuple[str, str]:
        """One evaluation + publish cycle. Returns (emitted_verdict, reason).

        Exposed so tests can drive ticks deterministically without
        waiting on the emit loop's sleep.
        """
        raw_verdict, reason = self.evaluate()
        emitted = self._apply_hysteresis(raw_verdict)
        await self._publish(emitted, reason)
        return emitted, reason

    async def _emit_loop(self) -> None:
        interval_s = self._emit_interval.total_seconds()
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — never crash the loop
                logger.warning(
                    "cio_health_evaluator_tick_failed",
                    extra={"error": str(exc)},
                )
            await asyncio.sleep(interval_s)
