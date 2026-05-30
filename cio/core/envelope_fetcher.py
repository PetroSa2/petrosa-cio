"""Envelope-fetch helper with bounded TTL cache (P4.6-AC3, #154, FR62).

Calls ``GET /api/envelopes/active/{key}`` on petrosa-data-manager to fetch
the active envelope for a ``strategy_or_portfolio_key``. The data-manager
endpoint returns the **highest-``version`` envelope regardless of
``source``** (#200), so this helper does **not** filter by source — the
operator-approved precedence is "highest version wins", by construction
of the append-only versioned store from petrosa-data-manager#188.

This module is intentionally light: no NATS subscription, no in-process
mutation API for envelopes. AC3.a's cache-bust on ``envelopes.changed`` is
deferred to a sibling leaf — for now the cache is TTL-only (60s default).
AC3.b "refuse the order with a non-silent error" raises
:class:`EnvelopeNotFoundError`; the calling code (cio orchestrator /
admission) is responsible for surfacing the alert and refusing the order.

Usage::

    fetcher = EnvelopeFetcher(data_manager_url="http://petrosa-data-manager:8000")
    try:
        env = await fetcher.get_active("strategy:momentum-v3")
    except EnvelopeNotFoundError:
        # AC3.b: no envelope at all for this key — refuse and alert.
        ...
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS: float = 60.0
DEFAULT_TIMEOUT_SECONDS: float = 10.0
ACTIVE_ENVELOPE_PATH = "/api/envelopes/active/"


class EnvelopeNotFoundError(LookupError):
    """Raised when data-manager has no envelope for the requested key (HTTP 404).

    Per AC3.b: callers (cio orchestrator / admission) MUST treat this as a
    refusal condition and surface a non-silent error (alert via FR66 channel,
    log at ERROR).
    """


class EnvelopeFetchError(RuntimeError):
    """Raised when the fetch failed for transport-level / 5xx reasons.

    Distinct from :class:`EnvelopeNotFoundError` (which is a definite
    "no envelope") — callers may choose to retry on this one.
    """


@dataclass
class _CacheEntry:
    envelope: dict[str, Any]
    fetched_at: float


class EnvelopeFetcher:
    """TTL-cached envelope fetcher backed by data-manager's read API.

    Thread/asyncio-safe for concurrent ``get_active`` calls on the same key
    via a per-instance lock — concurrent requests for the same key coalesce
    into a single upstream call.
    """

    def __init__(
        self,
        data_manager_url: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = data_manager_url.rstrip("/")
        self._ttl = float(ttl_seconds)
        self._timeout = float(timeout_seconds)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=self._timeout)
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def cache_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a copy of the cache state — used by ``/healthz/envelopes`` (AC3.f).

        Each entry reports the cached envelope plus its age in seconds, so an
        operator can spot stale (TTL-expired-but-not-evicted) entries.
        """
        now = time.monotonic()
        return {
            key: {
                "envelope_id": entry.envelope.get("envelope_id"),
                "version": entry.envelope.get("version"),
                "source": entry.envelope.get("source"),
                "age_seconds": round(now - entry.fetched_at, 3),
                "fresh": (now - entry.fetched_at) < self._ttl,
            }
            for key, entry in self._cache.items()
        }

    def invalidate(self, key: str | None = None) -> None:
        """Drop a single cache entry or (if ``key is None``) the whole cache.

        Used today by tests; the sibling leaf wiring ``envelopes.changed`` will
        call ``invalidate(key)`` from a NATS handler.
        """
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    async def get_active(self, key: str) -> dict[str, Any]:
        if not key:
            raise ValueError("envelope key must be non-empty")
        cached = self._cache.get(key)
        if cached is not None and (time.monotonic() - cached.fetched_at) < self._ttl:
            return cached.envelope
        async with self._lock:
            # Re-check inside lock — another coroutine may have populated it.
            cached = self._cache.get(key)
            if (
                cached is not None
                and (time.monotonic() - cached.fetched_at) < self._ttl
            ):
                return cached.envelope
            envelope = await self._fetch(key)
            self._cache[key] = _CacheEntry(
                envelope=envelope, fetched_at=time.monotonic()
            )
            return envelope

    async def _fetch(self, key: str) -> dict[str, Any]:
        url = self._base_url + ACTIVE_ENVELOPE_PATH + key
        try:
            response = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.error(
                "envelope_fetch_transport_error",
                extra={"key": key, "url": url, "error": str(exc)},
            )
            raise EnvelopeFetchError(
                f"transport error fetching envelope for {key!r}: {exc}"
            ) from exc

        if response.status_code == 404:
            logger.warning(
                "envelope_not_found",
                extra={"key": key, "url": url},
            )
            raise EnvelopeNotFoundError(
                f"no envelope exists for strategy_or_portfolio_key={key!r}"
            )
        if response.status_code >= 500:
            logger.error(
                "envelope_fetch_server_error",
                extra={
                    "key": key,
                    "status": response.status_code,
                    "body": response.text[:200],
                },
            )
            raise EnvelopeFetchError(
                f"data-manager returned HTTP {response.status_code} for {key!r}"
            )
        if response.status_code != 200:
            raise EnvelopeFetchError(
                f"data-manager returned unexpected HTTP {response.status_code} for {key!r}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise EnvelopeFetchError(
                f"data-manager returned non-JSON body for {key!r}: {exc}"
            ) from exc
        if not isinstance(body, dict):
            raise EnvelopeFetchError(
                f"data-manager returned non-object body for {key!r}: {type(body).__name__}"
            )
        return body
