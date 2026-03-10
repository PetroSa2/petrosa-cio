# DEPRECATION_NOTICE: This is a temporary stub for ShadowROIEngine for Fix 2. Pending real component relocation.

from __future__ import annotations

from typing import Any


class ShadowROIEngine:
    """
    Temporary stub for ShadowROIEngine when the Nurse package is unavailable.
    Provides a safe, loud UNAVAILABLE response for health/earnings checks.
    """

    async def get_earnings_summary(self, window_hours: int = 24 * 7) -> dict[str, Any]:
        """
        Returns a loudly unavailable status to prevent silent data errors.
        """
        return {
            "status": "UNAVAILABLE",
            "actual_pnl": 0.0,
            "shadow_roi": 0.0,
            "message": "NURSE_ROI_ENGINE_OFFLINE: This component is currently in maintenance. Contact Architecture for ROI-Enforcer status.",
        }
