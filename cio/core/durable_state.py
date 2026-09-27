"""Data-manager durable state client used by CIO."""

from __future__ import annotations

import json
from typing import Any

import httpx


class DataManagerStateStore:
    """Small client for the data-manager durable-state API."""

    def __init__(self, base_url: str, namespace: str, timeout: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        self._client = httpx.AsyncClient(timeout=timeout)

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
