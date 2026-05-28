"""LLM spend tracker — FR63 cost ceiling and operator visibility.

Accumulates cost per prompt_id (decision_type) and UTC calendar day. On each
call the orchestrator may check whether the projected daily spend has crossed
the configurable per-day ceiling; when it does, `check_ceiling` returns
`ceiling_breached=True` and the orchestrator transitions CIO to deterministic-
fallback mode (FR13) and fires a critical alert (FR66).

Recovery path: at the start of each new UTC day the tracker auto-resets its
period accumulator. When `ceiling_breached` transitions from True→False (new
period), the orchestrator re-enables `use_llm_reasoning=True`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

# ── Cost rates (per million tokens) ──────────────────────────────────────────
# Defaults match Claude Haiku pricing. Override via env to match actual model.
_INPUT_COST_PER_MILLION = float(os.getenv("LLM_COST_INPUT_PER_MILLION", "0.25"))
_OUTPUT_COST_PER_MILLION = float(os.getenv("LLM_COST_OUTPUT_PER_MILLION", "1.25"))

# Prompt-id → human-readable decision type label.
DECISION_TYPE_LABELS: dict[str, str] = {
    "PETROSA_PROMPT_REGIME_CLASSIFIER": "regime_classification",
    "PETROSA_PROMPT_STRATEGY_ASSESSOR": "strategy_assessment",
    "PETROSA_PROMPT_ACTION_CLASSIFIER": "action_classification",
}


def _tokens_to_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * _INPUT_COST_PER_MILLION
        + output_tokens / 1_000_000 * _OUTPUT_COST_PER_MILLION
    )


@dataclass
class BucketAccumulator:
    decision_type: str
    input_tokens: int = 0
    output_tokens: int = 0
    call_count: int = 0

    @property
    def cost_usd(self) -> float:
        return _tokens_to_usd(self.input_tokens, self.output_tokens)


@dataclass
class PeriodSpend:
    period_date: date
    buckets: dict[str, BucketAccumulator] = field(default_factory=dict)
    ceiling_usd_per_day: float = 5.0

    @property
    def total_cost_usd(self) -> float:
        return sum(b.cost_usd for b in self.buckets.values())

    @property
    def total_input_tokens(self) -> int:
        return sum(b.input_tokens for b in self.buckets.values())

    @property
    def total_output_tokens(self) -> int:
        return sum(b.output_tokens for b in self.buckets.values())

    def record(self, prompt_id: str, input_tokens: int, output_tokens: int) -> None:
        label = DECISION_TYPE_LABELS.get(prompt_id, prompt_id)
        if label not in self.buckets:
            self.buckets[label] = BucketAccumulator(decision_type=label)
        b = self.buckets[label]
        b.input_tokens += input_tokens
        b.output_tokens += output_tokens
        b.call_count += 1

    def projected_daily_usd(self) -> float:
        """Linearly project current spend to a full UTC day based on elapsed time."""
        now = datetime.now(UTC)
        period_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        elapsed_seconds = (now - period_start).total_seconds()
        if elapsed_seconds < 60:
            return self.total_cost_usd
        daily_seconds = 86_400.0
        return self.total_cost_usd * (daily_seconds / elapsed_seconds)

    def ceiling_breached(self) -> bool:
        return self.projected_daily_usd() >= self.ceiling_usd_per_day


class LlmSpendTracker:
    """Thread-safe (asyncio-safe) singleton for intra-process spend tracking."""

    _instance: LlmSpendTracker | None = None

    @classmethod
    def instance(cls) -> LlmSpendTracker:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._current: PeriodSpend = self._new_period()

    def _new_period(self) -> PeriodSpend:
        ceiling = float(os.getenv("LLM_SPEND_CEILING_USD_PER_DAY", "5.0"))
        return PeriodSpend(
            period_date=datetime.now(UTC).date(),
            ceiling_usd_per_day=ceiling,
        )

    def _maybe_roll_period(self) -> bool:
        """Roll to a new period if the UTC date has changed. Returns True on roll."""
        today = datetime.now(UTC).date()
        if today != self._current.period_date:
            self._current = self._new_period()
            return True
        return False

    def record(self, prompt_id: str, input_tokens: int, output_tokens: int) -> None:
        self._maybe_roll_period()
        self._current.record(prompt_id, input_tokens, output_tokens)

    def check_ceiling(self) -> tuple[bool, float, float]:
        """Return (ceiling_breached, total_cost_usd, projected_daily_usd)."""
        rolled = self._maybe_roll_period()
        if rolled:
            return False, 0.0, 0.0
        period = self._current
        return (
            period.ceiling_breached(),
            period.total_cost_usd,
            period.projected_daily_usd(),
        )

    def period_snapshot(self) -> dict:
        """Serialisable snapshot for the dashboard API."""
        self._maybe_roll_period()
        p = self._current
        return {
            "period_date": p.period_date.isoformat(),
            "ceiling_usd_per_day": p.ceiling_usd_per_day,
            "total_cost_usd": round(p.total_cost_usd, 6),
            "projected_daily_usd": round(p.projected_daily_usd(), 6),
            "ceiling_breached": p.ceiling_breached(),
            "distance_to_ceiling_usd": round(
                max(0.0, p.ceiling_usd_per_day - p.projected_daily_usd()), 6
            ),
            "buckets": [
                {
                    "decision_type": b.decision_type,
                    "cost_usd": round(b.cost_usd, 6),
                    "input_tokens": b.input_tokens,
                    "output_tokens": b.output_tokens,
                    "call_count": b.call_count,
                }
                for b in sorted(
                    p.buckets.values(), key=lambda x: x.cost_usd, reverse=True
                )
            ],
        }

    def reset_for_test(self, ceiling_usd: float = 5.0) -> None:
        """Only for tests — resets the singleton's period state."""
        self._current = PeriodSpend(
            period_date=datetime.now(UTC).date(),
            ceiling_usd_per_day=ceiling_usd,
        )
