"""Stale-characterization refusal gate (FR53 / P3.4 — petrosa-cio#130).

The CIO refuses any intent whose `strategy_revision_id` does not match a
persisted `Characterization` for the same `strategy_id`. The check is a
single HTTP GET against the data-manager endpoint introduced in
`petrosa-data-manager#179` (`GET /api/v1/characterizations?strategy_id=…
&strategy_revision_id=…`):

  * **200**  ⇒  a characterization exists for the exact revision → not stale.
  * **404**  ⇒  no characterization for that revision → **stale**, refuse.
  * Any other outcome (timeout, 5xx, connection error) is treated as
    **not stale** so a data-manager outage does not silently block every
    intent (fail-open). The outage is logged at WARNING for ops visibility.

Design notes
------------
* The check is intentionally narrow — it does NOT recompute hashes, mutate
  the characterization, or attempt to cache. The producer-side hash
  canonicalization is already byte-stable with the consumer (data-manager's
  `compute_inputs_hash`), so a string compare against the `404` signal is
  sufficient.
* `strategy_revision_id` is the only required input beyond `strategy_id`.
  If the intent does not carry one (legacy producer pre-P3.4), the gate
  returns `False` (not stale) — refusing every legacy intent would be a
  hostile rollout.
* The gate is `async` and uses a short timeout so it does not lengthen
  the cold path beyond its existing SLO.
"""

from __future__ import annotations

import logging
import os
from typing import Final

import httpx

logger = logging.getLogger(__name__)

DEFAULT_DATA_MANAGER_URL_ENV: Final[str] = "DATA_MANAGER_URL"
DEFAULT_DATA_MANAGER_URL: Final[str] = "http://petrosa-data-manager:8000"
DEFAULT_TIMEOUT_S: Final[float] = 2.0


def _base_url() -> str:
    return os.getenv(DEFAULT_DATA_MANAGER_URL_ENV, DEFAULT_DATA_MANAGER_URL).rstrip("/")


async def is_characterization_stale(
    *,
    strategy_id: str,
    strategy_revision_id: str | None,
    base_url: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Return True iff data-manager has no characterization for the (strategy,
    revision) pair — the operator-visible "stale, refuse" signal (FR53 / AC3).

    A missing or empty ``strategy_revision_id`` is **not** treated as stale —
    pre-P3.4 producers continue to pass through.
    """
    if not strategy_revision_id:
        return False

    url = (base_url or _base_url()) + "/api/v1/characterizations"
    params = {
        "strategy_id": strategy_id,
        "strategy_revision_id": strategy_revision_id,
    }

    async def _do(client_: httpx.AsyncClient) -> bool:
        try:
            resp = await client_.get(url, params=params, timeout=timeout_s)
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            logger.warning(
                "stale-characterization gate: data-manager unreachable — failing open",
                extra={
                    "strategy_id": strategy_id,
                    "strategy_revision_id": strategy_revision_id,
                    "url": url,
                    "error": str(exc),
                },
            )
            return False
        if resp.status_code == 404:
            logger.info(
                "stale-characterization gate: revision not found → refusing intent",
                extra={
                    "strategy_id": strategy_id,
                    "strategy_revision_id": strategy_revision_id,
                },
            )
            return True
        if resp.status_code == 200:
            return False
        # Any other status: log and fail open so the operator can investigate
        # without losing every intent in the meantime.
        logger.warning(
            "stale-characterization gate: unexpected status — failing open",
            extra={
                "strategy_id": strategy_id,
                "strategy_revision_id": strategy_revision_id,
                "status_code": resp.status_code,
            },
        )
        return False

    if client is not None:
        return await _do(client)
    async with httpx.AsyncClient() as owned:
        return await _do(owned)
