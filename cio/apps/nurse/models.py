"""
Nurse-specific safety models for the Petrosa CIO.
Changes to this file require MFA approval via GitHub branch protection.
"""

from pydantic import BaseModel, Field, model_validator

from cio.core.safety_constants import (
    HEARTBEAT_TIMEOUT_MS,
    MAX_DRAWDOWN_PCT,
    MAX_ORDERS_GLOBAL,
    MAX_ORDERS_PER_SYMBOL,
    MAX_POSITION_SIZE_PCT,
    VOLATILITY_SCALE_THRESHOLD,
)


class RiskLimits(BaseModel):
    """Hard risk limits enforced by the Code Engine."""

    max_drawdown_pct: float = Field(
        MAX_DRAWDOWN_PCT,
        ge=0.0,
        le=1.0,
        description="Maximum tolerated portfolio drawdown in fraction format.",
    )
    max_position_size_pct: float = Field(
        MAX_POSITION_SIZE_PCT,
        ge=0.0,
        le=1.0,
        description="Upper bound for a single position as fraction of capital.",
    )
    volatility_scale_threshold: float = Field(
        VOLATILITY_SCALE_THRESHOLD,
        ge=0.0,
        description="Volatility breach threshold that triggers position scaling.",
    )
    max_orders_global: int = Field(
        MAX_ORDERS_GLOBAL,
        description="Max concurrent open orders across the whole fleet.",
    )
    max_orders_per_symbol: int = Field(
        MAX_ORDERS_PER_SYMBOL,
        description="Max concurrent open orders for a single asset.",
    )
    max_position_size_usd: float = Field(
        5000.0,
        description="Hard absolute cap on any single position size in USD.",
    )
    # tradeengine reports the floor in PERCENT (6.0 = 6%), unlike its fraction-valued sibling
    # ``min_sl_entry_distance_pct``. ``min_sl_distance_frac`` is the same floor as a fraction of the entry,
    # converted here at the boundary (pct / 100), and is the only one the net-EV gate uses.
    min_sl_distance_pct: float | None = Field(
        None,
        description=(
            "The stop-distance floor tradeengine re-anchors every stop to, in PERCENT as reported on "
            "/state. Absent (older tradeengine) means unknown."
        ),
    )
    min_sl_distance_source: str | None = Field(
        None,
        description="Where the floor comes from, as tradeengine emits it: env | fallback | derived.",
    )
    min_sl_distance_frac: float | None = Field(
        None,
        description="min_sl_distance_pct / 100: the floor as a fraction of the entry.",
    )
    probe_mode: bool = Field(
        False,
        description=(
            "True when tradeengine sizes every entry to the symbol's smallest valid order; "
            "max_position_size_usd is then that probe notional. Absent (older tradeengine) means false."
        ),
    )

    @model_validator(mode="after")
    def _floor_fraction(self) -> "RiskLimits":
        """Convert tradeengine's percent floor to a fraction once, at the boundary."""
        pct = self.min_sl_distance_pct
        if self.min_sl_distance_frac is None and pct is not None and pct > 0:
            self.min_sl_distance_frac = pct / 100.0
        return self


class ExecutionPolicy(BaseModel):
    """Execution policy for strategist-directed trade orchestration."""

    allowed_modes: list[str] = Field(
        default_factory=lambda: ["deterministic", "ml_light"],
        description="Strategy modes allowed for autonomous execution.",
    )
    require_manual_approval: bool = Field(
        False,
        description="Whether manual approval is required before publishing signals.",
    )
    heartbeat_timeout_ms: int = Field(
        HEARTBEAT_TIMEOUT_MS,
        ge=10,
        description="Heartbeat timeout used by downstream services for fail-safe mode.",
    )
