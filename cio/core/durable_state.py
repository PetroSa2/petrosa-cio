"""Data-manager durable state client used by CIO."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# The auto-resume registry lives behind data-manager (cio#262). The original
# 2.0s client timeout was far below data-manager's observed tail latency for
# /api/v1/cio/auto-resume/entries (live 2026-09-28: p50 ~0.2s, p90 ~2.8s,
# p99 ~6.3s, max ~9.7s), so ~13% of registry calls raised httpx.ReadTimeout
# (AUTO_RESUME_REGISTRY_ERROR ... exc_type=ReadTimeout) and pause bookkeeping
# was silently dropped. Align with the other CIO -> data-manager clients
# (CIO_CONTEXT_FETCH_TIMEOUT_S defaults to 10s); keep connect failures fast.
STORE_TIMEOUT_ENV = "CIO_AUTO_RESUME_STORE_TIMEOUT_S"
DEFAULT_STORE_TIMEOUT_SECONDS = 10.0
STORE_CONNECT_TIMEOUT_SECONDS = 2.0


def resolve_store_timeout_seconds() -> float:
    """Read the registry read/write timeout, falling back to the safe default."""
    raw = os.getenv(STORE_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_STORE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        logger.warning(
            f"Invalid {STORE_TIMEOUT_ENV}={raw!r}; using "
            f"{DEFAULT_STORE_TIMEOUT_SECONDS}s"
        )
        return DEFAULT_STORE_TIMEOUT_SECONDS
    return value


class DataManagerStateStore:
    """Small client for the data-manager durable-state API."""

    def __init__(
        self, base_url: str, namespace: str, timeout: float | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        read_timeout = resolve_store_timeout_seconds() if timeout is None else timeout
        self.timeout = httpx.Timeout(
            read_timeout, connect=min(STORE_CONNECT_TIMEOUT_SECONDS, read_timeout)
        )
        self._client = httpx.AsyncClient(timeout=self.timeout)

    @property
    def _entries_url(self) -> str:
        return f"{self.base_url}/api/v1/cio/auto-resume/entries"

    @staticmethod
    def _without_metadata(entry: dict[str, Any]) -> str:
        entry = dict(entry)
        entry.pop("updated_at", None)
        entry.pop("_id", None)
        return json.dumps(entry)

    def _headers(self) -> dict[str, str]:
        return {"X-Petrosa-Issuer": "CIO", "X-Petrosa-Namespace": self.namespace}

    async def get(self, key: str) -> str | None:
        response = await self._client.get(
            f"{self._entries_url}/{key}", headers=self._headers()
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return self._without_metadata(response.json())

    async def all(self) -> dict[str, str]:
        response = await self._client.get(self._entries_url, headers=self._headers())
        response.raise_for_status()
        entries = response.json().get("entries", [])
        return {
            entry["strategy_id"]: self._without_metadata(entry) for entry in entries
        }

    async def put(self, key: str, value: str) -> None:
        entry = json.loads(value)
        entry["strategy_id"] = key
        response = await self._client.put(
            f"{self._entries_url}/{key}", json=entry, headers=self._headers()
        )
        response.raise_for_status()

    async def delete(self, key: str) -> None:
        response = await self._client.delete(
            f"{self._entries_url}/{key}", headers=self._headers()
        )
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
