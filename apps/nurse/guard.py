"""Market regime semantic guard for Nurse enforcement."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


class RegimePolicy(BaseModel):
    """Policy payload loaded from Redis `policy:regime`."""

    current_regime: str = "unknown"
    vol_threshold_breach: bool = False
    drawdown_limit_exceeded: bool = False
    current_drawdown: float = 0.0
    scale_down_factor: float = Field(default=0.5, ge=0.0, le=1.0)


@dataclass(slots=True)
class GuardDecision:
    """Outcome returned by semantic guard."""

    approved: bool
    scale_factor: float
    veto_reason: str | None
    metadata: dict[str, Any]
    saved_capital: float


class RegimeGuard:
    """Evaluates semantic risk rules using current market regime policy."""

    def __init__(
        self, redis_client: Any | None = None, redis_key: str = "policy:regime"
    ):
        self.redis_client = redis_client
        self.redis_key = redis_key

    async def get_current_regime(self) -> tuple[RegimePolicy, float]:
        """Fetch and parse current regime policy from Redis."""
        start = time.perf_counter()

        if self.redis_client is None:
            policy = RegimePolicy()
            return policy, (time.perf_counter() - start) * 1000.0

        raw = await self.redis_client.get(self.redis_key)
        if not raw:
            policy = RegimePolicy()
            return policy, (time.perf_counter() - start) * 1000.0

        if isinstance(raw, bytes):
            raw = raw.decode()

        payload = json.loads(raw)
        policy = RegimePolicy(**payload)
        return policy, (time.perf_counter() - start) * 1000.0

    async def evaluate(self, intent_payload: dict[str, Any]) -> GuardDecision:
        policy, lookup_ms = await self.get_current_regime()

        quantity = float(intent_payload.get("quantity", 0.0))
        current_drawdown = max(float(policy.current_drawdown), 0.0)

        metadata = {
            "current_regime": policy.current_regime,
            "vol_threshold_breach": policy.vol_threshold_breach,
            "drawdown_limit_exceeded": policy.drawdown_limit_exceeded,
            "regime_lookup_ms": lookup_ms,
        }

        if policy.drawdown_limit_exceeded:
            saved_capital = self.calculate_saved_capital(
                signal_size=quantity,
                current_drawdown=current_drawdown,
                scale_factor=0.0,
            )
            return GuardDecision(
                approved=False,
                scale_factor=0.0,
                veto_reason="drawdown_limit_exceeded",
                metadata=metadata,
                saved_capital=saved_capital,
            )

        if policy.vol_threshold_breach and quantity > 0:
            saved_capital = self.calculate_saved_capital(
                signal_size=quantity,
                current_drawdown=current_drawdown,
                scale_factor=policy.scale_down_factor,
            )
            return GuardDecision(
                approved=True,
                scale_factor=policy.scale_down_factor,
                veto_reason=None,
                metadata=metadata,
                saved_capital=saved_capital,
            )

        return GuardDecision(
            approved=True,
            scale_factor=1.0,
            veto_reason=None,
            metadata=metadata,
            saved_capital=0.0,
        )

    @staticmethod
    def calculate_saved_capital(
        signal_size: float,
        current_drawdown: float,
        scale_factor: float = 0.0,
    ) -> float:
        """Estimate capital preserved by block/scale decision."""
        reduced_fraction = max(1.0 - scale_factor, 0.0)
        return float(signal_size) * float(current_drawdown) * reduced_fraction
