"""Per-strategy rolling-hour new-notional concentration guard."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from cio.core.metrics import record_concentration_share

logger = logging.getLogger(__name__)

ROLLING_HOUR = timedelta(hours=1)
DEFAULT_CAP = 0.25


class ExecuteRecord(Protocol):
    strategy_id: str
    action: str
    timestamp: datetime
    new_notional_usd: float


@dataclass(frozen=True)
class ConcentrationConfig:
    cap: float
    cap_source: str
    mode: str
    mode_source: str


@dataclass(frozen=True)
class ConcentrationResult:
    strategy_id: str
    share: float
    cap: float
    cap_source: str
    mode: str
    result: str
    window_start: datetime
    total_notional_usd: float
    strategy_notional_usd: float

    @property
    def exceeded(self) -> bool:
        return self.share > self.cap

    def as_reason(self) -> dict[str, object]:
        return {
            "type": "concentration_cap",
            "strategy_id": self.strategy_id,
            "share": self.share,
            "cap": self.cap,
            "cap_source": self.cap_source,
            "mode": self.mode,
            "window_start": self.window_start.isoformat(),
            "total_notional_usd": self.total_notional_usd,
            "strategy_notional_usd": self.strategy_notional_usd,
        }


def load_config() -> ConcentrationConfig:
    raw_cap = os.getenv("CIO_CONCENTRATION_CAP_PCT")
    parsed_env_cap = False
    try:
        cap = float(raw_cap) if raw_cap is not None else DEFAULT_CAP
        parsed_env_cap = raw_cap is not None
    except (TypeError, ValueError):
        cap = DEFAULT_CAP
    valid_env_cap = parsed_env_cap and 0 < cap <= 1
    if not valid_env_cap:
        if raw_cap is not None:
            logger.warning(
                "CONFIG_WARNING: CIO_CONCENTRATION_CAP_PCT=%s is invalid; using labelled fallback",
                raw_cap,
            )
        cap = DEFAULT_CAP
    cap_source = "env" if valid_env_cap else "fallback"

    raw_mode_value = os.getenv("CIO_CONCENTRATION_CAP_MODE")
    raw_mode = (raw_mode_value or "shadow").strip().lower()
    if raw_mode not in {"shadow", "enforce"}:
        logger.warning(
            "CONFIG_WARNING: CIO_CONCENTRATION_CAP_MODE=%s is invalid; using shadow fallback",
            raw_mode_value,
        )
        return ConcentrationConfig(cap, cap_source, "shadow", "fallback")
    return ConcentrationConfig(
        cap, cap_source, raw_mode, "env" if raw_mode_value is not None else "fallback"
    )


def evaluate(
    *,
    strategy_id: str,
    proposed_notional_usd: float,
    records: Iterable[ExecuteRecord],
    at: datetime,
    config: ConcentrationConfig | None = None,
) -> ConcentrationResult:
    config = config or load_config()
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    window_start = at - ROLLING_HOUR
    total = 0.0
    strategy_total = 0.0
    for record in records:
        timestamp = record.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if not window_start <= timestamp <= at or record.action != "execute":
            continue
        notional = max(0.0, float(record.new_notional_usd))
        total += notional
        if record.strategy_id == strategy_id:
            strategy_total += notional
    proposed = max(0.0, float(proposed_notional_usd))
    total += proposed
    strategy_total += proposed
    share = strategy_total / total if total else 0.0
    result = "over_cap" if share > config.cap else "under_cap"
    record_concentration_share(strategy_id, share)
    from cio.core.metrics import CONCENTRATION_CAP_TOTAL

    CONCENTRATION_CAP_TOTAL.add(1, {"mode": config.mode, "result": result})
    return ConcentrationResult(
        strategy_id=strategy_id,
        share=share,
        cap=config.cap,
        cap_source=config.cap_source,
        mode=config.mode,
        result=result,
        window_start=window_start,
        total_notional_usd=total,
        strategy_notional_usd=strategy_total,
    )
