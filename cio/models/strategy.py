from datetime import datetime

from pydantic import BaseModel, Field

from cio.models.enums import (
    ActivationRecommendation,
    HealthStatus,
    ParamChangeDirection,
    RegimeFit,
)


class ParamChangeSignal(BaseModel):
    """Initial signal for a parameter change from the LLM."""

    param: str = Field(..., description="Parameter name as defined in strategy config")
    direction: ParamChangeDirection
    reason: str = Field(
        ..., max_length=80, description="Brief reason referencing health signals"
    )


class AppliedParamChange(BaseModel):
    """
    Concrete parameter change after being processed by the Decision Assembler.
    Includes the old and new values, plus audit fields.
    """

    strategy_id: str
    timestamp: datetime
    param: str
    old_value: float
    new_value: float
    direction: ParamChangeDirection
    reason: str


class StrategyResult(BaseModel):
    """
    Result of the Strategy Assessor persona's analysis.
    Evaluates strategy health and fit for the current regime.
    """

    health: HealthStatus
    regime_fit: RegimeFit
    activation_recommendation: ActivationRecommendation
    param_change: ParamChangeSignal | None = None
    thought_trace: str = Field(
        default="",
        max_length=120,
        description="1 sentence reasoning trace (optional for minimal LLM profile)",
    )
