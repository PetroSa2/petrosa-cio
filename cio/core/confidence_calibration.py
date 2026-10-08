"""Read-only confidence calibration calculations and data-manager adapter."""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)
_last_unavailable_warning = 0.0


@dataclass(frozen=True)
class CalibrationConfig:
    minimum_sample: int = 30
    reliability_bound: float = 0.10
    neutral_prior: float = 0.50

    @classmethod
    def from_env(cls) -> CalibrationConfig:
        return cls(
            minimum_sample=max(1, int(os.getenv("CIO_CALIBRATION_MIN_SAMPLE", "30"))),
            reliability_bound=max(
                0.0, float(os.getenv("CIO_CALIBRATION_RELIABILITY_BOUND", "0.10"))
            ),
            neutral_prior=min(
                1.0, max(0.0, float(os.getenv("CIO_CALIBRATION_NEUTRAL_PRIOR", "0.50")))
            ),
        )


def effective_confidence(raw: float, status: dict[str, Any] | None, config: CalibrationConfig | None = None) -> float:
    """Return raw confidence only for a calibrated strategy; otherwise return the neutral prior."""
    config = config or CalibrationConfig.from_env()
    if status and status.get("calibrated") is True:
        return min(1.0, max(0.0, float(raw)))
    global _last_unavailable_warning
    now = time.monotonic()
    if now - _last_unavailable_warning >= 60.0:
        logger.warning("Confidence calibration unavailable or uncalibrated; using neutral prior")
        _last_unavailable_warning = now
    return config.neutral_prior


def _win(record: dict[str, Any]) -> int:
    net = record.get("net_pnl")
    if net is None:
        gross = float(record.get("gross_pnl", 0.0))
        costs = float(record.get("costs", record.get("total_costs", 0.0)))
        net = gross - costs
    return int(float(net) > 0.0)


def _row(strategy: str, decile: int, records: list[dict[str, Any]], config: CalibrationConfig) -> dict[str, Any]:
    n = len(records)
    confidence = [float(item["confidence"]) for item in records]
    wins = [_win(item) for item in records]
    mean_confidence = sum(confidence) / n
    win_rate = sum(wins) / n
    reliability_error = abs(mean_confidence - win_rate)
    return {
        "strategy_id": strategy,
        "confidence_decile": decile,
        "n": n,
        "realized_net_win_rate": win_rate,
        "realized_net_expectancy": sum(float(item.get("net_pnl", float(item.get("gross_pnl", 0.0)) - float(item.get("costs", item.get("total_costs", 0.0))))) for item in records) / n,
        "brier_score": sum((confidence[i] - wins[i]) ** 2 for i in range(n)) / n,
        "mean_confidence": mean_confidence,
        "reliability_error": reliability_error,
        "sample_ok": n >= config.minimum_sample,
        "calibrated": n >= config.minimum_sample and reliability_error < config.reliability_bound,
        "outcome_source": "executed",
    }


def build_report(records: list[dict[str, Any]], config: CalibrationConfig | None = None) -> dict[str, Any]:
    """Build an executed-only calibration report without network or database access."""
    config = config or CalibrationConfig.from_env()
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        confidence = float(record["confidence"])
        if not 0.0 <= confidence <= 1.0:
            continue
        strategy = str(record.get("strategy_id", "unknown"))
        decile = min(10, int(confidence * 10) + 1)
        groups[(strategy, decile)].append(record)
    rows = [_row(strategy, decile, values, config) for (strategy, decile), values in groups.items()]
    rows.sort(key=lambda row: (row["sample_ok"], row["strategy_id"], row["confidence_decile"]))
    overall = _row("overall", 0, records, config) if records else {
        "strategy_id": "overall", "confidence_decile": 0, "n": 0,
        "realized_net_win_rate": None, "realized_net_expectancy": None,
        "brier_score": None, "mean_confidence": None, "reliability_error": None,
        "sample_ok": False, "calibrated": False, "outcome_source": "executed",
    }
    return {
        "outcome_source": "executed",
        "sample_size_minimum": config.minimum_sample,
        "reliability_bound": config.reliability_bound,
        "neutral_prior": config.neutral_prior,
        "groups": [overall, *rows],
    }


class ConfidenceCalibrationService:
    """Fetch executed records from data-manager and calculate a report."""

    def __init__(self, client: httpx.AsyncClient | None = None, data_manager_url: str | None = None):
        self.client = client
        self.data_manager_url = (data_manager_url or os.getenv("DATA_MANAGER_URL", "http://petrosa-data-manager:80")).rstrip("/")

    async def report(self) -> dict[str, Any]:
        async def fetch(client: httpx.AsyncClient) -> dict[str, Any]:
            response = await client.get(f"{self.data_manager_url}/analysis/calibration/confidence")
            response.raise_for_status()
            return response.json()

        if self.client is not None:
            payload = await fetch(self.client)
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                payload = await fetch(client)
        records = payload.get("records", payload.get("outcomes", []))
        return build_report(records if isinstance(records, list) else [])
