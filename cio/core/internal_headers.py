"""Shared authentication headers for CIO's internal HTTP clients."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TOKEN_ENV = "PETROSA_INTERNAL_TOKEN"
_DM_SERVICE_NAME_ENV = "DM_SERVICE_NAME"
_DM_SERVICE_TOKEN_ENV = "DM_SERVICE_TOKEN"
_DEFAULT_DM_SERVICE_NAME = "petrosa-cio"
_warned_missing_token = False
_warned_missing_dm_token = False


def internal_headers(*, namespace: str | None = None) -> dict[str, str]:
    """Build the legacy and data-manager authentication headers."""
    global _warned_missing_token, _warned_missing_dm_token
    token = os.getenv(_TOKEN_ENV, "")
    dm_service_name = os.getenv(_DM_SERVICE_NAME_ENV, _DEFAULT_DM_SERVICE_NAME)
    dm_service_token = os.getenv(_DM_SERVICE_TOKEN_ENV, "")
    headers = {
        "X-Petrosa-Issuer": "CIO",
        "X-Petrosa-Service": dm_service_name,
    }
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
    if dm_service_token:
        headers["Authorization"] = f"Bearer {dm_service_token}"
    elif not _warned_missing_dm_token:
        logger.warning(
            "SECURITY_WARNING: DM_SERVICE_TOKEN is not set; "
            "data-manager requests will identify the service without a bearer token."
        )
        _warned_missing_dm_token = True
    return headers
