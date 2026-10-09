"""Last-good reports with an age bound, a backoff and a background timeout (petrosa-cio#312 follow-up).

The decision path never awaits a data-manager call. A report is refreshed in the background; a failed refresh
keeps the last good report for as long as it is young enough, and retries with a growing delay. The age of a
report is judged from the time data-manager computed it when the body says so (``metadata.calculated_at``,
``metadata.computed_at``, ``metadata.timestamp`` or ``as_of``), and never less than the time since CIO fetched it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

DEFAULT_MAX_AGE_SECONDS = 6 * 3600.0
DEFAULT_FETCH_TIMEOUT_SECONDS = 30.0
BACKOFF_FIRST_SECONDS = 60.0
BACKOFF_CAP_SECONDS = 900.0


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    return value if value > 0 else default


def report_max_age_seconds() -> float:
    """The oldest a last-good report may be and still be used: ``CIO_REPORT_MAX_AGE_SECONDS``, 6 h (fallback)."""
    return _positive_float_env("CIO_REPORT_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS)


def report_fetch_timeout_seconds() -> float:
    """The timeout of a background report refresh: ``CIO_REPORT_FETCH_TIMEOUT_S``, 30 s (fallback).

    Separate from the decision-path timeout (``CIO_CONTEXT_FETCH_TIMEOUT_S``): no decision waits for a report.
    """
    return _positive_float_env(
        "CIO_REPORT_FETCH_TIMEOUT_S", DEFAULT_FETCH_TIMEOUT_SECONDS
    )


def backoff_seconds(failures: int) -> float:
    """The delay before the next refresh after ``failures`` failures in a row: 60 s, 2 min, 4 min, ... 15 min."""
    if failures <= 0:
        return BACKOFF_FIRST_SECONDS
    return min(BACKOFF_FIRST_SECONDS * 2 ** (failures - 1), BACKOFF_CAP_SECONDS)


def computed_at_of(body: Any) -> datetime | None:
    """The time data-manager computed the report, from its body; None when it does not say."""
    if not isinstance(body, dict):
        return None
    metadata = body.get("metadata")
    candidates: list[Any] = []
    if isinstance(metadata, dict):
        candidates += [
            metadata.get("calculated_at"),
            metadata.get("computed_at"),
            metadata.get("timestamp"),
        ]
    candidates.append(body.get("as_of"))
    for raw in candidates:
        if not raw:
            continue
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    return None


def report_age_seconds(
    *,
    since_fetch: float,
    computed_at: datetime | None,
    now: datetime | None = None,
) -> float:
    """The report's age: the larger of the time since CIO fetched it and the time since data-manager computed it."""
    age = max(0.0, since_fetch)
    if computed_at is not None:
        wall = (now or datetime.now(UTC)) - computed_at
        age = max(age, wall.total_seconds())
    return age
