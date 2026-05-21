"""Evaluator-verdict subscriber for CIO arbitration pause (P2.6, #597).

Subscribes to ``evaluator.>`` and tracks the latest verdict per subsystem.
The arbiter consults :meth:`is_paused` before allowing a signal through
so unhealthy upstream evaluators (ingest, strategies, audit, …) pause
new arbitration on the affected scope rather than emitting decisions on
stale or broken data (FR45).

State is per-process and in-memory: the next published verdict refreshes
it, and a restart re-syncs from the next evaluator tick on each subsystem
(NFR-R1's detection-time window applies). Persistent state was out of
scope for this ticket — pause events that need to survive a CIO restart
should be filed as a follow-up.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

if TYPE_CHECKING:
    from nats.aio.client import Client as NATS

logger = logging.getLogger(__name__)


# `evaluator.{subsystem}.verdict` per the P2.1 publisher contract.
EVALUATOR_SUBJECT_PATTERN = "evaluator.>"
HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"


class EvaluatorSubscriber:
    """Subscribes to ``evaluator.>`` and exposes the current pause set.

    Callers (``SignalArbiter`` today; an HTTP ``/state`` route too) only
    interact with the read API — :meth:`is_paused` and
    :meth:`paused_subsystems` — so the subject parsing + JSON decoding is
    contained here.
    """

    def __init__(
        self,
        nats_client: NATS,
        *,
        on_change: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> None:
        """
        Args:
            nats_client: connected nats-py client.
            on_change: optional async callback fired on every committed
                verdict change. Signature ``(subsystem, new_verdict,
                reason) -> Awaitable[None]``. Lets main.py persist a
                pause/resume audit trail without coupling the subscriber
                to that path.
        """
        self._nc = nats_client
        self._on_change = on_change
        # subsystem -> (verdict, reason, observed_at)
        self._verdicts: dict[str, tuple[str, str, datetime]] = {}
        # Operator-driven overrides — when set, force arbiter pause/resume
        # regardless of the subscriber's latest verdict. Stored as
        # subsystem -> override_verdict.
        self._overrides: dict[str, str] = {}
        self._subscription = None

    async def start(self) -> None:
        """Subscribe to ``evaluator.>``."""
        self._subscription = await self._nc.subscribe(
            EVALUATOR_SUBJECT_PATTERN,
            cb=self._handle_message,
        )
        logger.info(
            "evaluator_subscriber_started",
            extra={"subject": EVALUATOR_SUBJECT_PATTERN},
        )

    async def stop(self) -> None:
        if self._subscription is not None:
            try:
                await self._subscription.unsubscribe()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"evaluator subscriber unsubscribe failed: {exc}")
            self._subscription = None

    async def _handle_message(self, msg) -> None:
        # Subject form: evaluator.{subsystem}.verdict — extract the
        # subsystem token defensively; an unexpected shape just gets
        # logged and dropped so the subscriber survives malformed traffic.
        parts = msg.subject.split(".")
        if len(parts) < 3 or parts[0] != "evaluator" or parts[-1] != "verdict":
            logger.warning(
                "evaluator_subject_malformed",
                extra={"subject": msg.subject},
            )
            return
        subsystem = ".".join(parts[1:-1])

        try:
            payload = json.loads(msg.data.decode())
        except (json.JSONDecodeError, AttributeError) as exc:
            logger.warning(
                "evaluator_payload_unparsable",
                extra={"subject": msg.subject, "error": str(exc)},
            )
            return

        verdict = payload.get("verdict")
        reason = (payload.get("reason") or "")[:200]
        if verdict not in (HEALTHY, UNHEALTHY, UNKNOWN):
            logger.warning(
                "evaluator_verdict_invalid",
                extra={"subject": msg.subject, "verdict": verdict},
            )
            return

        previous = self._verdicts.get(subsystem)
        now = datetime.now(UTC)
        self._verdicts[subsystem] = (verdict, reason, now)

        if previous is None or previous[0] != verdict:
            logger.info(
                "evaluator_verdict_changed",
                extra={
                    "subsystem": subsystem,
                    "verdict": verdict,
                    "reason": reason,
                    "previous": previous[0] if previous else None,
                },
            )
            if self._on_change is not None:
                try:
                    await self._on_change(subsystem, verdict, reason)
                except Exception as exc:  # noqa: BLE001 — never crash subscribe
                    logger.warning(f"on_change callback failed: {exc}")

    def is_paused(self, subsystem: str) -> bool:
        """True iff CIO arbitration should pause on this subsystem.

        Operator overrides win; otherwise pause when the latest verdict
        is unhealthy. ``unknown`` and ``healthy`` allow arbitration to
        continue — operators tune the override to handle "unknown is
        bad" semantics on a case-by-case basis.
        """
        override = self._overrides.get(subsystem)
        if override is not None:
            return override == UNHEALTHY
        record = self._verdicts.get(subsystem)
        if record is None:
            return False
        return record[0] == UNHEALTHY

    def paused_subsystems(self) -> list[dict]:
        """Snapshot of paused subsystems for the /state endpoint."""
        result = []
        # Union of observed-verdict subsystems and operator-override
        # subsystems — operators may pause something that hasn't emitted
        # a verdict yet, and that pause must still surface.
        all_keys = set(self._verdicts.keys()) | set(self._overrides.keys())
        for subsystem in sorted(all_keys):
            record = self._verdicts.get(subsystem)
            verdict = record[0] if record else None
            reason = record[1] if record else ""
            observed_at = record[2].isoformat() if record else None
            override = self._overrides.get(subsystem)
            effective = override if override is not None else verdict
            if effective != UNHEALTHY:
                continue
            result.append(
                {
                    "subsystem": subsystem,
                    "verdict": verdict,
                    "reason": reason,
                    "observed_at": observed_at,
                    "override": override,
                }
            )
        return result

    def set_override(self, subsystem: str, verdict: str | None) -> None:
        """Operator override — set to None to clear.

        Surfaces in :meth:`paused_subsystems` so the dashboard can show
        a manual pause/unpause separately from evaluator-driven state.
        """
        if verdict is None:
            self._overrides.pop(subsystem, None)
            return
        if verdict not in (HEALTHY, UNHEALTHY, UNKNOWN):
            raise ValueError(
                f"override verdict must be one of healthy/unhealthy/unknown, got {verdict!r}"
            )
        self._overrides[subsystem] = verdict

    def snapshot(self) -> dict:
        """Full state — used by the /state endpoint to expose verdict
        history for every observed subsystem (paused or not)."""
        out = []
        for subsystem, (verdict, reason, observed_at) in self._verdicts.items():
            out.append(
                {
                    "subsystem": subsystem,
                    "verdict": verdict,
                    "reason": reason,
                    "observed_at": observed_at.isoformat(),
                    "override": self._overrides.get(subsystem),
                }
            )
        return {"verdicts": out, "paused": self.paused_subsystems()}
