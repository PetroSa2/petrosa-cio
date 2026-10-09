"""Read-only confidence calibration calculations and data-manager adapter."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict

import httpx

from cio.core.metrics import REPORT_AGE, REPORT_REFRESH
from cio.core.report_freshness import (
    backoff_seconds,
    computed_at_of,
    report_age_seconds,
    report_max_age_seconds,
)

logger = logging.getLogger(__name__)
_last_unavailable_warning = 0.0


class CalibrationOutcomeRecord(TypedDict, total=False):
    """Data-manager ``GET /analysis/calibration/confidence`` record contract."""

    strategy_id: str
    confidence: float
    net_pnl: float
    gross_pnl: float
    costs: float
    total_costs: float


class CalibrationGroup(TypedDict):
    strategy_id: str
    confidence_decile: int
    n: int
    realized_net_win_rate: float | None
    realized_net_expectancy: float | None
    brier_score: float | None
    mean_confidence: float | None
    reliability_error: float | None
    sample_ok: bool
    calibrated: bool
    outcome_source: Literal["executed"]


class CalibrationReport(TypedDict):
    """Read-only report contract returned by the CIO calibration endpoint."""

    outcome_source: Literal["executed"]
    sample_size_minimum: int
    reliability_bound: float
    neutral_prior: float
    groups: list[CalibrationGroup]


def _records_from_response(payload: dict[str, Any]) -> list[CalibrationOutcomeRecord]:
    """Validate the data-manager response contract at the HTTP boundary.

    The response has one required top-level field, ``records``. Each record must
    contain ``strategy_id`` and ``confidence`` plus either ``net_pnl`` or the
    gross/cost pair. No simulated or alternate payload shape is accepted.
    """
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("calibration response records must be a list")
    normalized: list[CalibrationOutcomeRecord] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("calibration records must be objects")
        if not isinstance(record.get("strategy_id"), str):
            raise ValueError("calibration record strategy_id must be a string")
        if not isinstance(record.get("confidence"), int | float):
            raise ValueError("calibration record confidence must be numeric")
        has_net = isinstance(record.get("net_pnl"), int | float)
        has_gross_and_costs = all(
            isinstance(record.get(field), int | float)
            for field in ("gross_pnl", "costs")
        )
        if not has_net and not has_gross_and_costs:
            raise ValueError(
                "calibration record requires net_pnl or gross_pnl and costs"
            )
        normalized.append(record)
    return normalized


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


CalibrationMode = Literal["log_only", "enforce"]


def calibration_mode() -> CalibrationMode:
    """Return the operator-controlled caller mode, defaulting to diagnostic-only."""
    return (
        "enforce"
        if os.getenv("CIO_CALIBRATION_MODE", "log_only").strip().lower() == "enforce"
        else "log_only"
    )


def effective_confidence(
    raw: float, status: dict[str, Any] | None, config: CalibrationConfig | None = None
) -> float:
    """Return raw confidence only for a calibrated strategy; otherwise return the neutral prior."""
    config = config or CalibrationConfig.from_env()
    if status and status.get("calibrated") is True:
        return min(1.0, max(0.0, float(raw)))
    global _last_unavailable_warning
    now = time.monotonic()
    if now - _last_unavailable_warning >= 60.0:
        logger.warning(
            "Confidence calibration unavailable or uncalibrated; using neutral prior"
        )
        _last_unavailable_warning = now
    return config.neutral_prior


def _win(record: dict[str, Any]) -> int:
    net = record.get("net_pnl")
    if net is None:
        gross = float(record.get("gross_pnl", 0.0))
        costs = float(record.get("costs", record.get("total_costs", 0.0)))
        net = gross - costs
    return int(float(net) > 0.0)


def _row(
    strategy: str, decile: int, records: list[dict[str, Any]], config: CalibrationConfig
) -> dict[str, Any]:
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
        "realized_net_expectancy": sum(
            float(
                item.get(
                    "net_pnl",
                    float(item.get("gross_pnl", 0.0))
                    - float(item.get("costs", item.get("total_costs", 0.0))),
                )
            )
            for item in records
        )
        / n,
        "brier_score": sum((confidence[i] - wins[i]) ** 2 for i in range(n)) / n,
        "mean_confidence": mean_confidence,
        "reliability_error": reliability_error,
        "sample_ok": n >= config.minimum_sample,
        "calibrated": n >= config.minimum_sample
        and reliability_error < config.reliability_bound,
        "outcome_source": "executed",
    }


def build_report(
    records: list[dict[str, Any]], config: CalibrationConfig | None = None
) -> dict[str, Any]:
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
    rows = [
        _row(strategy, decile, values, config)
        for (strategy, decile), values in groups.items()
    ]
    rows.sort(
        key=lambda row: (row["sample_ok"], row["strategy_id"], row["confidence_decile"])
    )
    overall = (
        _row("overall", 0, records, config)
        if records
        else {
            "strategy_id": "overall",
            "confidence_decile": 0,
            "n": 0,
            "realized_net_win_rate": None,
            "realized_net_expectancy": None,
            "brier_score": None,
            "mean_confidence": None,
            "reliability_error": None,
            "sample_ok": False,
            "calibrated": False,
            "outcome_source": "executed",
        }
    )
    return {
        "outcome_source": "executed",
        "sample_size_minimum": config.minimum_sample,
        "reliability_bound": config.reliability_bound,
        "neutral_prior": config.neutral_prior,
        "groups": [overall, *rows],
    }


DEFAULT_CALIBRATION_TTL_SECONDS = 900.0
DEFAULT_CALIBRATION_TIMEOUT_SECONDS = 120.0


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    return value if value > 0 else default


def calibration_ttl_seconds() -> float:
    """How long a calibration report is reused before the next background refresh:
    ``CIO_CALIBRATION_TTL_SECONDS``, 900 s."""
    return _positive_float_env(
        "CIO_CALIBRATION_TTL_SECONDS", DEFAULT_CALIBRATION_TTL_SECONDS
    )


def calibration_timeout_seconds() -> float:
    """The timeout of the background calibration refresh: ``CIO_CALIBRATION_TIMEOUT_SECONDS``, 120 s.

    No decision waits for it (the refresh runs in the background); the endpoint is slow (minutes) today.
    """
    return _positive_float_env(
        "CIO_CALIBRATION_TIMEOUT_SECONDS", DEFAULT_CALIBRATION_TIMEOUT_SECONDS
    )


class ConfidenceCalibrationService:
    """Fetch executed records from data-manager and calculate a report.

    ``report()`` is a fresh read (the calibration endpoint). The decision path reads ``status()``, which uses
    ``cached_report()``: one fetch per TTL with a short timeout (petrosa-cio#312: no data-manager call per
    decision). A failed fetch keeps the last good report; with none, the status is None, and the fetch is not
    retried until the TTL passes. The unavailable warning is logged once per TTL.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        data_manager_url: str | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.client = client
        self.data_manager_url = (
            data_manager_url
            or os.getenv("DATA_MANAGER_URL", "http://petrosa-data-manager:80")
        ).rstrip("/")
        self._clock = clock or time.monotonic
        # the last good report, when CIO fetched it, when data-manager computed it; the next refresh is not
        # before ``_retry_at``; a refresh in flight is shared by every decision
        self._good: CalibrationReport | None = None
        self._good_at: float | None = None
        self._good_computed_at: datetime | None = None
        self._retry_at = 0.0
        self._failures = 0
        self._task: asyncio.Future[None] | None = None
        self._too_old_logged = False
        self._last_computed_at: datetime | None = None

    async def report(self, timeout: float | None = None) -> CalibrationReport:
        async def fetch(client: httpx.AsyncClient) -> dict[str, Any]:
            response = await client.get(
                f"{self.data_manager_url}/analysis/calibration/confidence",
                **({"timeout": timeout} if timeout is not None else {}),
            )
            response.raise_for_status()
            return response.json()

        if self.client is not None:
            payload = await fetch(self.client)
        else:
            async with httpx.AsyncClient(timeout=timeout or 15.0) as client:
                payload = await fetch(client)
        self._last_computed_at = computed_at_of(payload)
        return build_report(_records_from_response(payload))

    def cached_report(self) -> CalibrationReport | None:
        """The report for the decision path. **Never awaits a data-manager call** (petrosa-cio#312 follow-up).

        Returns the last good report while it is younger than ``CIO_REPORT_MAX_AGE_SECONDS`` (else None: the neutral
        prior) and, when a refresh is due, starts one in the background (one at a time).
        """
        self._start_refresh_if_due()
        return self._usable_report()

    def _usable_report(self) -> CalibrationReport | None:
        if self._good is None or self._good_at is None:
            return None
        age = report_age_seconds(
            since_fetch=self._clock() - self._good_at,
            computed_at=self._good_computed_at,
        )
        REPORT_AGE.record(age, {"report": "calibration"})
        if age > report_max_age_seconds():
            if not self._too_old_logged:
                self._too_old_logged = True
                logger.warning(
                    "REPORT_TOO_OLD report=calibration age_s=%.0f max_age_s=%.0f: using the neutral prior",
                    age,
                    report_max_age_seconds(),
                )
            return None
        return self._good

    def _start_refresh_if_due(self) -> None:
        if self._clock() < self._retry_at:
            return
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop: nothing to refresh on
            return
        self._task = loop.create_task(self.refresh())

    def warm(self) -> None:
        """Start the first refresh now (at startup), off the decision path. Needs the running loop."""
        self._start_refresh_if_due()

    async def wait_refresh(self) -> None:
        """Wait for the refresh in flight, if any (tests and shutdown; the decision path never calls this)."""
        task = self._task
        if task is not None and not task.done():
            await asyncio.shield(task)

    async def refresh(self) -> None:
        """Fetch the report and keep it; a failure keeps the last good one and backs off (60 s ... 15 min)."""
        ttl = calibration_ttl_seconds()
        try:
            payload_report = await self._fetch(timeout=calibration_timeout_seconds())
        except Exception as exc:  # noqa: BLE001 - calibration cannot stop decisions
            self._failures += 1
            delay = backoff_seconds(self._failures)
            self._retry_at = self._clock() + delay
            REPORT_REFRESH.add(1, {"report": "calibration", "outcome": "failed"})
            logger.warning(
                "Confidence calibration report unavailable (exc_type=%s %s); %s, next attempt in %.0fs",
                type(exc).__name__,
                exc,
                "using the last good report"
                if self._good is not None
                else "using the neutral prior",
                delay,
            )
            return
        report, computed_at = payload_report
        self._good = report
        self._good_at = self._clock()
        self._good_computed_at = computed_at
        self._too_old_logged = False
        self._failures = 0
        self._retry_at = self._clock() + ttl
        REPORT_REFRESH.add(1, {"report": "calibration", "outcome": "ok"})

    async def _fetch(self, timeout: float) -> tuple[CalibrationReport, Any]:
        report = await self.report(timeout=timeout)
        return report, getattr(self, "_last_computed_at", None)

    async def status(self, strategy_id: str) -> dict[str, Any] | None:
        """Return the strategy's calibration status from the shared report contract."""
        report = self.cached_report()
        if report is None:
            return None
        for group in report["groups"]:
            if group.get("strategy_id") == strategy_id:
                return group
        return None
