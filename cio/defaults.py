"""Default strategist governance schemas exposed via MCP tools."""

from pydantic import BaseModel, Field


class RiskLimits(BaseModel):
    """Risk limits controlling exposure and drawdown safeguards."""

    max_drawdown_pct: float = Field(
        0.2,
        ge=0.0,
        le=1.0,
        description="Maximum tolerated portfolio drawdown in fraction format.",
    )
    max_position_size_pct: float = Field(
        0.1,
        ge=0.0,
        le=1.0,
        description="Upper bound for a single position as fraction of capital.",
    )
    volatility_scale_threshold: float = Field(
        0.03,
        ge=0.0,
        description="Volatility breach threshold that triggers position scaling.",
    )


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
        200,
        ge=10,
        description="Heartbeat timeout used by downstream services for fail-safe mode.",
    )
