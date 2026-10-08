"""The daily keep/kill job (petrosa-cio#299, rule 10).

Reads every strategy's net-of-cost closed rounds from data-manager's scorecard, takes the equity and the
drawdown reduce-step (rule 5) from the same sources the decision path uses, and writes one record per strategy
with its inputs and sources. A kill goes through the existing ``pause_strategy`` path (the router) and is logged;
in the default ``log_only`` mode nothing is paused. The per-strategy probation budget is kept for the cold-start
limit of the net-EV gate (petrosa-cio#296).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from cio.core.keep_kill import (
    KeepKillRecord,
    StrategyScore,
    evaluate_strategies,
    keep_kill_mode,
)
from cio.models import ActionType, TriggerType
from cio.models.decision import DecisionResult
from cio.models.enums import (
    ActivationRecommendation,
    ConfidenceLevel,
    HealthStatus,
    RegimeFit,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 24 * 3600.0
SCORECARD_PATH = "/api/v1/analysis/strategy-net-r"

#: ``() -> (equity_usd, reduce_step as a fraction of equity)``; either may be None when unavailable
RiskSnapshot = Callable[[], Awaitable[tuple[float | None, float | None]]]


def parse_scorecard(body: dict[str, Any]) -> list[StrategyScore]:
    """The scorecard response: ``{"strategies": {id: {"net_r": [...], "cumulative_net_loss_usd", "closed_rounds"}}}``."""
    scores: list[StrategyScore] = []
    for strategy_id, item in (body.get("strategies") or {}).items():
        rounds = [float(r) for r in (item or {}).get("net_r") or [] if r is not None]
        loss = (item or {}).get("cumulative_net_loss_usd")
        closed = (item or {}).get("closed_rounds")
        scores.append(
            StrategyScore(
                strategy_id=str(strategy_id),
                net_r=rounds,
                cumulative_net_loss_usd=float(loss) if loss is not None else None,
                closed_rounds=int(closed) if closed is not None else None,
            )
        )
    return scores


class KeepKillJob:
    """Evaluates every strategy once a day and pauses a killed one (when enforcing)."""

    def __init__(
        self,
        http_client: Any,
        data_manager_url: str,
        risk_snapshot: RiskSnapshot,
        pause: Callable[[str, str], Awaitable[None]] | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._http = http_client
        self._dm = data_manager_url
        self._risk = risk_snapshot
        self._pause = pause
        self._interval = interval_seconds
        self.records: dict[str, KeepKillRecord] = {}
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    def probation_budget(self, strategy_id: str) -> float | None:
        """The strategy's loss budget in USD while it is on probation (None otherwise or unknown)."""
        record = self.records.get(strategy_id)
        if record is None or not record.on_probation:
            return None
        return record.probation_budget_usd

    async def run_once(self) -> list[KeepKillRecord]:
        try:
            response = await self._http.get(f"{self._dm}{SCORECARD_PATH}")
            response.raise_for_status()
            scores = parse_scorecard(response.json())
        except Exception as exc:
            logger.warning("KEEP_KILL scorecard not available: %s", exc)
            return []
        try:
            equity, reduce_step = await self._risk()
        except Exception as exc:
            logger.warning("KEEP_KILL risk snapshot not available: %s", exc)
            equity, reduce_step = None, None
        records = evaluate_strategies(
            scores, reduce_step=reduce_step, equity_usd=equity
        )
        self.records = {r.strategy_id: r for r in records}
        mode = keep_kill_mode()
        for record in records:
            logger.info(
                "KEEP_KILL %s %s",
                record.strategy_id,
                json.dumps(record.as_dict(), default=str, sort_keys=True),
            )
            if record.action == "kill":
                logger.warning(
                    "KEEP_KILL kill %s (%s) mode=%s",
                    record.strategy_id,
                    record.reason,
                    mode,
                )
                if mode == "enforce" and self._pause is not None:
                    try:
                        await self._pause(
                            record.strategy_id,
                            f"keep_kill: {record.reason} (n={record.n}, "
                            f"P(net EV>0)={record.prob_net_ev_positive}, "
                            f"loss=${record.cumulative_net_loss_usd}, budget=${record.probation_budget_usd})",
                        )
                    except Exception:
                        logger.exception(
                            "KEEP_KILL pause failed for %s", record.strategy_id
                        )
        return records

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("KEEP_KILL run failed")
            # Wait on the stop event, not a bare sleep: a stop wakes the loop at once, and a patched or
            # instant sleep (as in the main() tests) cannot turn it into a hot loop.
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                return
            except TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


def make_pause(
    context_builder: Any, router: Any
) -> Callable[[str, str], Awaitable[None]]:
    """Pause a strategy through the router's existing ``pause_strategy`` path."""

    async def pause(strategy_id: str, reason: str) -> None:
        correlation_id = f"keep-kill-{strategy_id}-{uuid.uuid4().hex[:8]}"
        context = await context_builder.build(
            correlation_id=correlation_id,
            source_subject="cio.keep_kill",
            trigger_type=TriggerType.SCHEDULED_REVIEW,
            payload={"strategy_id": strategy_id, "reeval_reason": "keep_kill"},
        )
        decision = DecisionResult(
            hard_blocked=False,
            ev_passes=False,
            cost_viable=False,
            regime_confidence=ConfidenceLevel.LOW,
            regime_fit=RegimeFit.NEUTRAL,
            strategy_health=HealthStatus.FAILING,
            activation_recommendation=ActivationRecommendation.PAUSE,
            action=ActionType.PAUSE_STRATEGY,
            justification=reason,
            thought_trace="KEEP_KILL",
        )
        await router.route(context, decision)
        logger.warning(
            "KEEP_KILL paused %s: %s",
            strategy_id,
            reason,
            extra={"correlation_id": correlation_id},
        )

    return pause
