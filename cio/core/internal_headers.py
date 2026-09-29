"""Shared authentication headers for CIO's internal HTTP clients."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TOKEN_ENV = "PETROSA_INTERNAL_TOKEN"
_warned_missing_token = False


def internal_headers(*, namespace: str | None = None) -> dict[str, str]:
    """Build internal request headers without exposing a missing token value."""
    global _warned_missing_token
    token = os.getenv(_TOKEN_ENV, "")
    headers = {"X-Petrosa-Issuer": "CIO"}
    if namespace:
        headers["X-Petrosa-Namespace"] = namespace
    if token:
        headers["X-Petrosa-Internal-Token"] = token
    elif not _warned_missing_token:
        logger.warning(
            "SECURITY_WARNING: PETROSA_INTERNAL_TOKEN is not set; "
            "internal HTTP requests will be unauthenticated."
        )
        _warned_missing_token = True
    return headers
